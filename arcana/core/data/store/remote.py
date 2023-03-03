import os
import os.path as op
from pathlib import Path
import time
import logging
import errno
import json
import re
import shutil
import attrs
from fileformats.core import DataType, FileSet, Field
from arcana.core.utils.misc import (
    dir_modtime,
    JSON_ENCODING,
    append_suffix,
)
from arcana.core.exceptions import (
    ArcanaError,
    DatatypeUnsupportedByStoreError,
)
from arcana.core.utils.misc import dict_diff
from ..entry import DataEntry
from ..row import DataRow
from .base import DataStore


logger = logging.getLogger("arcana")

special_char_re = re.compile(r"[^a-zA-Z_0-9]")
tag_parse_re = re.compile(r"\((\d+),(\d+)\)")

RELEVANT_DICOM_TAG_TYPES = set(("UI", "CS", "DA", "TM", "SH", "LO", "PN", "ST", "AS"))

# COMMAND_INPUT_TYPES = {bool: "bool", str: "string", int: "number", float: "number"}


@attrs.define
class RemoteStore(DataStore):
    """
    Access class for XNAT data repositories

    Parameters
    ----------
    server : str (URI)
        URI of XNAT server to connect to
    project_id : str
        The ID of the project in the XNAT repository
    cache_dir : str (name_path)
        Path to local directory to cache remote data in
    user : str
        Username with which to connect to XNAT with
    password : str
        Password to connect to the XNAT repository with
    race_condition_delay : int
        The amount of time to wait before checking that the required
        fileset has been downloaded to cache by another process has
        completed if they are attempting to download the same fileset
    """

    server: str = attrs.field()
    cache_dir: str = attrs.field(converter=Path)
    name: str = None
    user: str = attrs.field(default=None, metadata={"asdict": False})
    password: str = attrs.field(default=None, metadata={"asdict": False})
    race_condition_delay: int = attrs.field(default=30)

    CHECKSUM_SUFFIX = ".md5.json"
    PROV_SUFFIX = ".__prov__.json"
    FIELD_PROV_RESOURCE = "__provenance__"
    METADATA_RESOURCE = "__arcana__"
    LICENSE_RESOURCE = "LICENSES"
    SITE_LICENSES_DATASET_ENV = "ARCANA_SITE_LICENSE_DATASET"
    SITE_LICENSES_USER_ENV = "ARCANA_SITE_LICENSE_USER"
    SITE_LICENSES_PASS_ENV = "ARCANA_SITE_LICENSE_PASS"

    ####################
    # Attrs validators #
    ####################

    @cache_dir.validator
    def cache_dir_validator(self, _, cache_dir):
        if not cache_dir.exists():
            raise ValueError(f"Cache dir, '{cache_dir}' does not exist")

    ################################
    # Abstractmethods to implement #
    ################################

    # populate_tree

    # populate_row

    # save_dataset_definition

    # load_dataset_definition

    # connect

    # disconnect

    # put_provenance

    # get_provenance

    #######################
    # Methods to override #
    #######################

    # The following methods can be thought of as "abstractmethods", the only
    # reason they aren't implemented as such is to give the option to override
    # the outer abstract method from DataStore directory, e.g. "post_fileset"
    # without having to provide a stub for the inner method ("upload_files") as well

    def download_files(self, entry: DataEntry, download_dir: Path, target_path: Path):
        raise NotImplementedError(f"`download_files` is not implemented by {self}")

    def upload_files(self, fileset: FileSet, entry: DataEntry):
        raise NotImplementedError(f"`upload_files` is not implemented by {self}")

    def download_value(self, field):
        """
        Extract and return the value of the field from the store

        Parameters
        ----------
        field : Field
            The field to retrieve the value for

        Returns
        -------
        value : int | float | str | ty.List[int] | ty.List[float] | ty.List[str]
            The value of the Field
        """
        raise NotImplementedError(f"`download_value` is not implemented by {self}")

    def upload_value(self, field, value):
        """
        Inserts or updates the field's value in the store

        Parameters
        ----------
        field : Field
            The field to insert into the store
        """
        raise NotImplementedError(f"`upload_value` is not implemented by {self}")

    def create_fileset_entry(self, path: str, datatype: type, row: DataRow):
        raise NotImplementedError(
            f"`create_fileset_entry` is not implemented by {self}"
        )

    def create_field_entry(self, path: str, datatype: type, row: DataRow):
        raise NotImplementedError("`create_field_entry` is not implemented by {self}")

    def get_checksums(self, uri: str) -> dict[str, str]:
        """
        Downloads the checksum digests associated with the files in the file-set.
        These are saved with the downloaded files in the cache and used to
        check if the files have been updated on the server

        Parameters
        ----------
        uri: str
            uri of the data item to download the checksums for
        """
        raise NotImplementedError(f"get_checksums needs to be implemented for {self}")

    def calculate_checksums(self, fileset: FileSet) -> dict[str, str]:
        """
        Downloads the checksum digests associated with the files in the file-set.
        These are saved with the downloaded files in the cache and used to
        check if the files have been updated on the server

        Parameters
        ----------
        uri: str
            uri of the data item to download the checksums for
        """
        raise NotImplementedError(
            f"calculate_checksums needs to be implemented for {self}"
        )

    ################################
    # Abstractmethod implementations
    ################################

    def get(self, entry: DataEntry, datatype: type) -> DataType:
        if entry.datatype.is_fileset:
            item = self.get_fileset(entry, datatype)
        elif entry.datatype.is_field:
            item = self.get_field(entry, datatype)
        else:
            raise DatatypeUnsupportedByStoreError(entry.datatype, self)
        assert isinstance(item, datatype)
        return item

    def put(self, item: DataType, entry: DataEntry):
        if entry.datatype.is_fileset:
            item = self.put_fileset(item, entry)
        elif entry.datatype.is_field:
            item = self.put_field(item, entry)
        else:
            raise DatatypeUnsupportedByStoreError(entry.datatype, self)
        return item

    def post(self, item: DataType, path: str, datatype: type, row: DataRow):
        if datatype.is_fileset:
            entry = self.post_fileset(item, path=path, datatype=datatype, row=row)
        elif datatype.is_field:
            entry = self.post_field(item, path=path, datatype=datatype, row=row)
        else:
            raise DatatypeUnsupportedByStoreError(datatype, self)
        return entry

    def get_fileset(self, entry: DataEntry, datatype: type) -> FileSet:
        """
        Caches a fileset to the local file system and returns the path to
        the cached files

        Parameters
        ----------
        entry: DataEntry
            The entry to retrieve the file-set for
        datatype: type
            the datatype to return the item as

        Returns
        -------
        FileSet
            the cached file-set
        """
        logger.info(
            "Getting %s from %s:%s row via API access",
            entry.path,
            entry.row.frequency,
            entry.row.id,
        )
        cache_path = self.cache_path(entry.uri)
        need_to_download = True
        if op.exists(cache_path):
            md5_path = append_suffix(cache_path, self.CHECKSUM_SUFFIX)
            if md5_path.exists():
                with open(md5_path, "r") as f:
                    cached_checksums = json.load(f)
            if cached_checksums == entry.checksums:
                need_to_download = False
        if need_to_download:
            with self.connection:
                download_dir = append_suffix(cache_path, ".download")
                try:
                    os.makedirs(download_dir)
                except OSError as e:
                    if e.errno == errno.EEXIST:
                        # Attempt to make tmp download directory. This will
                        # fail if another process (or previous attempt) has
                        # already created it. In that case this process will
                        # wait 'race_cond_delay' seconds to see if it has been
                        # updated (i.e. is being downloaded by the other process)
                        # and otherwise assume that it was interrupted and redownload.
                        self._delayed_download(
                            entry,
                            download_dir,
                            cache_path,
                            delay=self._race_cond_delay,
                        )
                    else:
                        raise
                else:
                    self.download_files(entry, download_dir, cache_path)
                    shutil.rmtree(download_dir)
                # Save checksums for future reference, so we can check to see if cache
                # is stale
                checksums = self.get_checksums(entry.uri)
                with open(
                    str(cache_path) + self.CHECKSUM_SUFFIX, "w", **JSON_ENCODING
                ) as f:
                    json.dump(checksums, f, indent=2)
        return datatype(cache_path.iterdir())

    def put_fileset(self, fileset: FileSet, entry: DataEntry) -> FileSet:
        """
        Stores files for a file set into the XNAT repository

        Parameters
        ----------
        fileset : FileSet
            The file-set to put the paths for
        fspaths: list[Path or str  ]
            The paths of files/directories to put into the XNAT repository

        Returns
        -------
        list[Path]
            The locations of the locally cached paths
        """

        # Create cache path
        cache_path = self.cache_path(entry.uri)
        if cache_path.exists():
            shutil.rmtree(cache_path)
        # Copy to cache
        cached = fileset.copy_to(cache_path, make_dirs=True, trim=True)
        self.upload_files(cache_path, entry)
        checksums = self.get_checksums(entry.uri)
        calculated_checksums = self.calculate_checksums(cached)
        if checksums != calculated_checksums:
            raise ArcanaError(
                f"Checksums for uploaded file-set at {entry} don't match that of the original files:\n\n"
                + dict_diff(
                    calculated_checksums,
                    checksums,
                    label1="original",
                    label2="remote",
                )
            )
        # Save checksums, to avoid having to redownload if they haven't been altered
        # on XNAT
        with open(
            append_suffix(cache_path, self.CHECKSUM_SUFFIX), "w", **JSON_ENCODING
        ) as f:
            json.dump(checksums, f, indent=2)
        logger.info(
            "Put %s into %s:%s row via API access",
            entry.path,
            entry.row.frequency,
            entry.row.id,
        )
        return cached

    def post_fileset(
        self, fileset: DataType, path: str, datatype: type, row: DataRow
    ) -> DataEntry:
        """
        Creates a new resource entry to store the fileset in then puts it in it

        Parameters
        ----------
        fileset : FileSet
            The file-set to put the paths for
        fspaths: list[Path or str  ]
            The paths of files/directories to put into the XNAT repository

        Returns
        -------
        list[Path]
            The locations of the locally cached paths
        """
        with self.connection:
            entry = self.create_fileset_entry(path, datatype, row)
            self.put_fileset(fileset, entry)

    def get_field(self, entry: DataEntry, datatype: type) -> Field:
        """
        Retrieves a fields value

        Parameters
        ----------
        field : Field
            The field to retrieve

        Returns
        -------
        value : ty.Union[float, int, str, ty.List[float], ty.List[int], ty.List[str]]
            The value of the field
        """
        return datatype(self.download_value(entry))

    def put_field(self, field: Field, entry: DataEntry):
        """Store the value for a field in the XNAT repository

        Parameters
        ----------
        field : Field
            the field to store the value for
        value : str or float or int or bool
            the value to store
        """
        return self.upload_value(entry.datatype(field).value)

    def post_field(
        self, field: Field, path: str, datatype: type, row: DataRow
    ) -> DataEntry:
        entry = self.create_field_entry(path, datatype, row)
        self.put_field(field, entry)
        return entry

    def site_licenses_dataset(self):
        """Return a dataset that holds site-wide licenses

        Returns
        -------
        Dataset or None
            the dataset that holds site-wide licenses
        """
        try:
            user = os.environ[self.SITE_LICENSES_USER_ENV]
        except KeyError:
            store = self
        else:
            # Reconnect to store with site-license user/password
            store = type(self)(
                server=self,
                cache_dir=self.cache_dir,
                user=user,
                password=os.environ[self.SITE_LICENSES_PASS_ENV],
            )
        try:
            dataset_name = os.environ[self.SITE_LICENSES_DATASET_ENV]
        except KeyError:
            return None
        return store.load_dataset(dataset_name)

    ##################
    # Helper methods #
    ##################

    def _delayed_download(
        self, entry: DataEntry, download_dir: Path, target_path: Path, delay: int
    ):
        logger.info(
            "Waiting %s seconds for incomplete download of '%s' "
            "initiated another process to finish",
            delay,
            target_path,
        )
        initial_mod_time = dir_modtime(download_dir)
        time.sleep(delay)
        if op.exists(target_path):
            logger.info(
                "The download of '%s' has completed "
                "successfully in the other process, continuing",
                target_path,
            )
            return
        elif initial_mod_time != dir_modtime(download_dir):
            logger.info(
                "The download of '%s' hasn't completed yet, but it has"
                " been updated.  Waiting another %s seconds before "
                "checking again.",
                target_path,
                delay,
            )
            self._delayed_download(entry, download_dir, target_path, delay)
        else:
            logger.warning(
                "The download of '%s' hasn't updated in %s "
                "seconds, assuming that it was interrupted and "
                "restarting download",
                target_path,
                delay,
            )
            shutil.rmtree(download_dir)
            os.mkdir(download_dir)
            self.download_files(entry, download_dir, target_path)

    def cache_path(self, uri: str):
        """Path to the directory where the item is/should be cached. Note that
        the URI of the item needs to be set beforehand

        Parameters
        ----------
        uri :  `str`
            the uri of the entry to be cached

        Returns
        -------
        cache_path : Path
            the path to the directory where the entry will be cached
        """
        return self.cache_dir.joinpath(*uri.split("/")[3:])

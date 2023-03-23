from __future__ import annotations
import logging
import typing as ty
import re
import attrs
import attrs.filters
from arcana.core.utils.misc import NestedContext
from arcana.core.data.space import DataSpace
from arcana.core.exceptions import (
    ArcanaNameError,
    ArcanaDataTreeConstructionError,
    ArcanaUsageError,
)
from .row import DataRow

if ty.TYPE_CHECKING:  # pragma: no cover
    from .set.base import Dataset


logger = logging.getLogger("arcana")


@attrs.define
class DataTree(NestedContext):

    dataset: Dataset = None
    root: DataRow = None

    def enter(self):
        assert self.root is None
        self._set_root()
        self.dataset.store.populate_tree(self)

    def exit(self):
        self.root = None

    @property
    def dataset_id(self):
        return self.dataset.id

    @property
    def hierarchy(self):
        return self.dataset.hierarchy

    def add_leaf(self, tree_path, additional_ids=None):
        """Creates a new row at a the path down the tree of the dataset as
        well as all "parent" rows upstream in the data tree

        Parameters
        ----------
        tree_path : list[str]
            The sequence of labels for each layer in the hierarchy of the
            dataset leading to the current row.
        additional_ids : dict[DataSpace, str]
            IDs for frequencies not in the dataset hierarchy that are to be
            set explicitly

        Raises
        ------
        ArcanaBadlyFormattedIDError
            raised if one of the IDs doesn't match the pattern in the
            `id_patterns`
        ArcanaDataTreeConstructionError
            raised if one of the groups specified in the ID inference reg-ex
            doesn't match a valid row_frequency in the data dimensions
        """
        if self.root is None:
            self._set_root()
        if additional_ids is None:
            additional_ids = {}
        # Get basis frequencies covered at the given depth of the
        if len(tree_path) != len(self.dataset.hierarchy):
            raise ArcanaDataTreeConstructionError(
                f"Tree path ({tree_path}) should have the same length as "
                f"the hierarchy ({self.dataset.hierarchy}) of {self}"
            )
        # Set a default ID of None for all parent frequencies that could be
        # inferred from a row at this depth
        # ids = {f: None for f in self.dataset.space}
        ids = dict(zip(self.dataset.hierarchy, tree_path))
        # Infer IDs and add them to those explicitly in the hierarchy
        ids.update(self.dataset.infer_ids(ids))
        # Calculate the combined freqs after each layer is added
        row_frequency = self.dataset.space(0)
        for layer_str in self.dataset.hierarchy:
            layer_freq = self.dataset.space[layer_str]
            # If all the axes introduced by the layer not present in parent layers
            # and none of the IDs of these axes have been inferred from other IDs,
            # then the ID of the axis out of the layer's axes with the least-
            # significant bit can be considered to be equivalent to the
            # ID of the layer and the IDs of the other axes of the layer set to None
            # (the order of # the bits in the DataSpace class should be arranged to
            # account for this default behaviour).
            #
            # For example, given a hierarchy of ['subject', 'session'] in the `Clinical`
            # data space, no groups are assumed to be present by default (i.e. if not
            # specified by the `id_patterns` attr of the dataset), and the `member`
            # ID is assumed to be equivalent to the `subject` ID, since `member`
            # correspdonds to the least significant bit in the value of the subject in
            # the `Clinical` data space enum.
            #
            # Conversely, the timepoint can't be assumed to be equal to the `session`
            # ID, since the session ID could be expected to also contain both the `member` and
            # `group` ID in it, and should be explicitly extracted by via `id_patterns`
            #
            #       session ID: MRH010_CONTROL03_MR02
            #
            # with the '02' part representing as the timepoint can be extracted with the
            #
            #       id_inference = {
            #           'timepoint': r'session:id:.*MR(0-9+)$'
            #       }
            layer_span = [str(f) for f in layer_freq.span()]
            if not (layer_freq & row_frequency) and not any(
                f in ids for f in layer_span
            ):
                for freq in layer_span[:-1]:
                    ids[freq] = None
                ids[layer_span[-1]] = ids[layer_str]
            row_frequency |= layer_freq
        assert row_frequency == max(self.dataset.space)
        # Set or override any inferred IDs within the ones that have been
        # explicitly provided
        clashing_ids = set(ids) & set(additional_ids)
        if clashing_ids:
            raise ArcanaUsageError(
                f"Additional IDs clash with those inferred: {clashing_ids}"
            )
        ids.update(additional_ids)
        # Create composite IDs for non-basis frequencies if they are not
        # explicitly in the layer dimensions
        for freq in set(self.dataset.space) - set(row_frequency.span()):
            freq_str = str(freq)
            if freq_str not in ids:
                id = tuple(ids[str(b)] for b in freq.span() if ids[str(b)] is not None)
                if id:
                    if len(id) == 1:
                        id = id[0]
                    ids[freq_str] = id
        # Determine whether leaf node is included in the dataset definition according
        # to the include and exclude criteria
        add_row = True
        for freq, include in self.dataset.include.items():
            freq_id = ids[freq]
            if (isinstance(include, list) and freq_id not in include) or (
                isinstance(include, str) and not re.match(include, freq_id)
            ):
                add_row = False
                logger.debug(
                    f"skipping adding leaf at {tree_path} as {str(freq)} ID "
                    f"'{freq_id}' is not explicitly included: {include}"
                )
        for freq, exclude in self.dataset.exclude.items():
            freq_id = ids[freq]
            if (isinstance(exclude, list) and freq_id in exclude) or (
                isinstance(exclude, str) and re.match(exclude, freq_id)
            ):
                add_row = False
                logger.debug(
                    f"skipping adding leaf at {tree_path} as {str(freq)} ID "
                    f"'{freq_id}' is explicitly excluded: {exclude}"
                )
        if add_row:
            return self._add_row(
                {f: ids.get(str(f)) for f in self.dataset.space}, row_frequency
            )

    def _add_row(self, ids: dict[DataSpace, str], row_frequency):
        """Adds a row to the dataset, creating all parent "aggregate" rows
        (e.g. for each subject, group or timepoint) where required

        Parameters
        ----------
        ids : dict[DataSpace, str]
            ids of the row in all frequencies that it intersects
        row: DataRow
            The row to add into the data tree

        Raises
        ------
        ArcanaDataTreeConstructionError
            If inserting a multiple IDs of the same class within the tree if
            one of their ids is None
        """
        # logger.debug(
        #     "Found %s row in %s dataset: %s", row_frequency, self.dataset_id, ids
        # )
        row_frequency = self.dataset.parse_frequency(row_frequency)
        row = DataRow(ids=ids, frequency=row_frequency, dataset=self.dataset)
        # Create new data row
        row_dict = self.root.children[row.frequency]
        if row.id in row_dict:
            raise ArcanaDataTreeConstructionError(
                f"ID clash ({row.id}) between rows inserted into data " "tree"
            )
        row_dict[row.id] = row
        # Insert root row
        # Insert parent rows if not already present and link them with
        # inserted row
        for parent_freq, parent_id in row.ids.items():
            if not parent_freq:
                continue  # Don't need to insert root row again
            diff_freq = (row.frequency ^ parent_freq) & row.frequency
            if diff_freq:
                # logger.debug(f'Linking parent {parent_freq}: {parent_id}')
                try:
                    parent_row = self.dataset.row(parent_freq, parent_id)
                except ArcanaNameError:
                    # logger.debug(
                    #     f'Parent {parent_freq}:{parent_id} not found, adding')
                    parent_ids = {
                        f: i
                        for f, i in row.ids.items()
                        if (f.is_parent(parent_freq) or f == parent_freq)
                    }
                    parent_row = self._add_row(parent_ids, parent_freq)
                # Set reference to level row in new row
                diff_id = row.frequency_id(diff_freq)
                children_dict = parent_row.children[row_frequency]
                if diff_id in children_dict:
                    raise ArcanaDataTreeConstructionError(
                        f"ID clash ({diff_id}) between rows inserted into "
                        f"data tree in {diff_freq} children of {parent_row} "
                        f"({children_dict[diff_id]} and {row}). You may "
                        f"need to set the `id_patterns` attr of the dataset "
                        "to disambiguate ID components (e.g. how to extract "
                        "the timepoint ID from a session label)"
                    )
                children_dict[diff_id] = row
        return row

    def _set_root(self):
        self.root = DataRow(
            ids={self.dataset.root_freq: None},
            frequency=self.dataset.root_freq,
            dataset=self.dataset,
        )

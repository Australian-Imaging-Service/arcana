import pytest
from arcana.core.data.set.base import Dataset
from arcana.core.analysis.salience import ColumnSalience
from arcana.core.data.quality import DataQuality
from arcana.testing import TestDataSpace
from arcana.core.cli.dataset import define, add_source, add_sink, missing_items
from fileformats.text import Plain as Text
from arcana.core.utils.misc import show_cli_trace


ARBITRARY_INTS_A = [234221, 93380, 43271, 137483, 30009, 214205, 363526]
ARBITRARY_INTS_B = [353726, 29202, 32867, 129872, 12281, 776524, 908763]


def get_arbitrary_slice(i, dim_length):
    a = ARBITRARY_INTS_A[i] % dim_length
    b = ARBITRARY_INTS_B[i] % dim_length
    lower = min(a, b)
    upper = max(a, b) + 1
    return lower, upper


def test_add_column_cli(saved_dataset, cli_runner):
    # Get CLI name for dataset (i.e. file system path prepended by 'dirtree//')
    # Add source to loaded dataset
    saved_dataset.add_source(
        name="a_source",
        path="file1",
        datatype=Text,
        row_frequency=TestDataSpace.d,
        quality_threshold=DataQuality.questionable,
        order=1,
        required_metadata={},
        is_regex=False,
    )
    # Add source column to saved dataset
    result = cli_runner(
        add_source,
        [
            saved_dataset.locator,
            "a_source",
            "fileformats.text:Plain",
            "--path",
            "file1",
            "--row-frequency",
            "d",
            "--quality",
            "questionable",
            "--order",
            "1",
            "--no-regex",
        ],
    )
    assert result.exit_code == 0, show_cli_trace(result)
    # Add source to loaded dataset
    saved_dataset.add_sink(
        name="a_sink",
        path="deriv",
        datatype=Text,
        row_frequency=TestDataSpace.d,
        salience=ColumnSalience.qa,
    )
    result = cli_runner(
        add_sink,
        [
            saved_dataset.locator,
            "a_sink",
            "fileformats.text:Plain",
            "--path",
            "deriv",
            "--row-frequency",
            "d",
            "--salience",
            "qa",
        ],
    )
    assert result.exit_code == 0, show_cli_trace(result)
    # Reload the saved dataset and check the parameters were saved/loaded
    # correctly
    loaded_dataset = Dataset.load(saved_dataset.locator)
    assert saved_dataset.columns == loaded_dataset.columns


@pytest.mark.skip("Not implemented")
def test_add_missing_items_cli(saved_dataset, cli_runner):
    result = cli_runner(missing_items, [])
    assert result.exit_code == 0, show_cli_trace(result)


def test_define_cli(dataset: Dataset, cli_runner):
    blueprint = dataset.__annotations__["blueprint"]
    # Get CLI name for dataset (i.e. file system path prepended by 'file//')
    path = dataset.locator
    # Start generating the arguments for the CLI
    args = list(blueprint.hierarchy)
    # Generate "arbitrary" values for included and excluded from dim length
    # and index
    included = {}
    excluded = {}
    for i, (dim_length, axis) in enumerate(zip(blueprint.dim_lengths, dataset.space)):
        a, b = get_arbitrary_slice(i, dim_length)
        if i % 2:
            included[str(axis)] = f"{a}:{b}"
        elif (b - a) < dim_length:  # Check that we aren't excluding all
            excluded[str(axis)] = f"{a}:{b}"
    # Add include and exclude options
    for axis, slce in included.items():
        args.extend(["--include", axis, slce])
    for axis, slce in excluded.items():
        args.extend(["--exclude", axis, slce])
    args.extend(["--space", "arcana.testing:TestDataSpace"])
    # Run the command line
    result = cli_runner(define, [path, *args])
    # Check tool completed successfully
    assert result.exit_code == 0, show_cli_trace(result)
    # Reload the saved dataset and check the parameters were saved/loaded
    # correctly
    loaded_dataset = Dataset.load(path)
    assert loaded_dataset.hierarchy == blueprint.hierarchy
    assert loaded_dataset.include == included
    assert loaded_dataset.exclude == excluded

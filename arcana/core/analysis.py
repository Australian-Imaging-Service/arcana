from abc import ABCMeta
import typing as ty
import inspect
from copy import copy, deepcopy
from collections import defaultdict
from itertools import chain
import operator as operator_module
from operator import attrgetter
import attrs
from .data.space import DataSpace
from .data.column import DataColumn
from .data.set import Dataset
from .enum import CheckSalience, ColumnSalience, ParameterSalience
from .utils import (
    PIPELINE_ANNOTATIONS,
    SWICTH_ANNOTATIONS,
    CHECK_ANNOTATIONS,
    ATTR_TYPE,
)
from arcana.exceptions import ArcanaDesignError


@attrs.define(frozen=True)
class Operation:
    """Defines logical expressions used in specifying conditions when different versions
    of pipelines will run"""

    operator: str
    operands: ty.Tuple[str]

    def evaluate(self, analysis, dataset):
        operands = [o.evaluate(analysis, dataset) for o in self.operands]
        if self.operator == "value_of":
            assert len(operands) == 1
            val = getattr(analysis, operands[0])
        elif self.operator == "is_provided":
            assert len(operands) <= 2
            column_name = getattr(analysis, operands[0])
            if column_name is None:
                val = False
            else:
                column = dataset[column_name]
                if len(operands) == 2:
                    in_format = operands[1]
                    val = column.format is in_format or issubclass(
                        column.format, in_format
                    )
                else:
                    val = True
        else:
            val = getattr(operator_module, self.operator)(*operands)
        return val


@attrs.define(frozen=True)
class ColumnSpec:
    """Specifies a column that the analysis can add when it is applied to a dataset"""

    name: str
    type: type
    desc: str
    row_frequency: DataSpace
    salience: ColumnSalience
    defined_in: ty.List[type]
    modified: ty.Tuple[ty.Tuple[str, ty.Any]]
    mapped_from: ty.Tuple[
        str, str or ty.Tuple
    ] or None = None  # sub-analysis name, column name

    def select_pipeline_builders(self, analysis, dataset):
        candidates = [
            p for p in analysis.__spec__.pipeline_builders if self.name in p.outputs
        ]
        selected = [
            m
            for m in candidates
            if (
                m.condition is not None
                and m.condition.evaluate(self, analysis, dataset)
            )
        ]
        # Check for defaults
        if not selected:
            selected = [p for p in candidates if p.condition is None]
        # Select pipeline builders from subanalysis if present
        if not selected and self.mapped_from is not None:
            subanalysis = getattr(analysis, self.mapped_from[0])
            sub_column_spec = subanalysis.__spec__.column(self.mapped_from[1])
            selected = sub_column_spec.select_pipeline_builders(subanalysis, dataset)

        if not selected:
            raise ArcanaDesignError(
                "Could not find any potential pipeline builders with conditions that "
                "match the current analysis parameterisation and provided dataset. "
                f"All candidates are: {candidates}"
            )
        # Check to see whether there are pipelines with the same switch
        all_switches = [p.switch for p in selected]
        if with_duplicate_switches := [
            p for p in selected if all_switches.count(p.switch)
        ]:
            raise ArcanaDesignError(
                "Multiple potential pipelines match criteria for the given analysis "
                f"configuration and provided dataset: {with_duplicate_switches}"
            )
        return selected


@attrs.define(frozen=True)
class Parameter:
    """Specifies a free parameter of an analysis"""

    name: str
    type: type
    desc: str
    salience: ParameterSalience
    choices: ty.Tuple[int] or ty.Tuple[float] or ty.Tuple[str] or None
    lower_bound: int or float or None
    upper_bound: int or float or None
    defined_in: ty.List[type]
    modified: ty.Tuple[ty.Tuple[str, ty.Any]]
    default: int or float or str or ty.Tuple[int] or ty.Tuple[float] or ty.Tuple[
        str
    ] = attrs.field()
    mapped_from: ty.Tuple[str, str] or None = None  # sub-analysis name, column name

    @default.validator
    def default_validator(self, _, default):
        if default is None and self.salience != ParameterSalience.required:
            raise ValueError(
                f"Default value for '{self.name}' parameter must be provided unless "
                f"parameter salience is set to'{ParameterSalience.required}'"
            )


@attrs.define(frozen=True)
class Switch:
    """Specifies a "switch" point at which the processing can bifurcate to handle two
    separate types of input streams"""

    name: str
    desc: str
    inputs: ty.Tuple[str]
    parameters: ty.Tuple[str]
    method: ty.Callable
    defined_in: type


@attrs.define(frozen=True)
class PipelineBuilder:
    """Specifies a method that is used to add nodes in the construction of a pipeline
    that is able to generate data for sink columns under certain conditions"""

    name: str
    desc: str
    parameters: ty.Tuple[str]
    inputs: ty.Tuple[str]
    outputs: ty.Tuple[str]
    condition: Operation or None
    switch: Switch or None
    method: ty.Callable
    defined_in: type


@attrs.define(frozen=True)
class Check:
    """Specifies a quality-control check that can be run on generated derivatives to
    assess the probability that they have failed"""

    name: str
    column: ColumnSpec
    desc: str
    inputs: ty.Tuple[str]
    parameters: ty.Tuple[str]
    salience: CheckSalience
    method: ty.Callable
    defined_in: type


@attrs.define(frozen=True)
class SubanalysisSpec:
    """Specifies a "sub-analysis" component, when composing an analysis of several
    predefined analyses"""

    name: str
    desc: str
    type: type
    mappings: ty.Tuple[str, str]  # to name in subanalysis, from name in analysis class
    defined_in: ty.List[type]

    def mapping(self, name):
        try:
            mapped_name = next(m[1] for m in self.mappings if name == m[0])
        except StopIteration:
            raise KeyError(f"No mapping from '{name}' in sub-analysis: {self.mappings}")
        return mapped_name


@attrs.define
class Subanalysis:
    """Wrapper around the actual analysis class of the subanalysis, which performs the
    mapping of attributes"""

    _spec: SubanalysisSpec
    _analysis: ty.Any  # initialised analysis class for the subanalysis
    _parent: ty.Any  # reference back to the parent analysis class

    def __getattr__(self, name: str) -> ty.Any:
        try:
            return object.__getattr__(self, name)
        except AttributeError:
            pass
        try:
            mapped_name = self._spec.mapping(name)
        except KeyError:
            value = getattr(self._analysis, name)
        else:
            value = getattr(self._parent, mapped_name)
        return value

    def __setattr__(self, name, value):
        if name in ("_spec", "_analysis", "_parent"):
            object.__setattr__(self, name, value)
        else:
            try:
                mapped_name = self._spec.mapping(name)
            except KeyError:
                pass
            else:
                raise AttributeError(
                    f"Cannot set value of attribute '{name}' in '{self._spec.name}' "
                    f"sub-analysis as it is mapped to '{mapped_name}' in the parent "
                    f"analysis {self._parent}"
                )
            setattr(self._analysis, name, value)


@attrs.define(frozen=True)
class SubanalysisDefault:
    """A callable class (substitutable for a function but with a state) that is used to
    automatically create the subanalysis objects when the parent analysis class is created"""

    name: str

    def __call__(self, parent):
        spec = parent.__spec__.subanalysis(self.name)
        return Subanalysis(
            spec=spec,
            analysis=spec.type(**{m[0]: attrs.NOTHING for m in spec.mappings}),
            parent=parent,
        )


def unique_names(inst, attr, val):
    names = [v.name for v in val]
    if duplicates := [v for v in val if names.count(v.name) > 1]:
        raise ValueError(f"Duplicate names found in provided tuple: {duplicates}")


@attrs.define(frozen=True)
class AnalysisSpec:
    """Specifies all the components of the analysis class"""

    space: type
    column_specs: ty.Tuple[ColumnSpec] = attrs.field(validator=unique_names)
    pipeline_builders: ty.Tuple[PipelineBuilder] = attrs.field(validator=unique_names)
    parameters: ty.Tuple[Parameter] = attrs.field(validator=unique_names)
    switches: ty.Tuple[Switch] = attrs.field(validator=unique_names)
    checks: ty.Tuple[Check] = attrs.field(validator=unique_names)
    subanalyses: ty.Tuple[SubanalysisSpec] = attrs.field(validator=unique_names)

    @property
    def column_names(self):
        return (c.name for c in self.column_specs)

    @property
    def parameter_names(self):
        return (p.name for p in self.parameters)

    @property
    def pipeline_names(self):
        return (p.name for p in self.pipeline_builders)

    @property
    def switch_names(self):
        return (s.name for s in self.switches)

    @property
    def check_names(self):
        return (c.name for c in self.checks)

    @property
    def subanalysis_names(self):
        return (s.name for s in self.subanalyses)

    def column_spec(self, name):
        return next(c for c in self.column_specs if c.name == name)

    def parameter(self, name):
        return next(p for p in self.parameters if p.name == name)

    def pipeline_builder(self, name):
        return next(p for p in self.pipeline_builders if p.name == name)

    def switch(self, name):
        return next(s for s in self.switches if s.name == name)

    def check(self, name):
        return next(c for c in self.checks if c.name == name)

    def subanalysis(self, name):
        return next(s for s in self.subanalyses if s.name == name)

    def column_checks(self, column_name):
        "Return all checks for a given column"
        return (c for c in self.checks if c.column == column_name)

    @column_specs.validator
    def column_specs_validator(self, _, column_specs):
        for column_spec in column_specs:
            sorted_by_cond = defaultdict(list)
            for pipe_spec in self.pipeline_builders:
                if column_spec.name in pipe_spec.outputs:
                    sorted_by_cond[(pipe_spec.condition, pipe_spec.switch)].append(
                        pipe_spec
                    )
            if duplicated := [
                (c, d) for (c, d) in sorted_by_cond.items() if len(d) > 1
            ]:
                raise ArcanaDesignError(
                    f"Multiple pipelines provide outputs for '{column_spec.name}' under "
                    "matching conditions - \n"
                    + "\n".join(
                        f"Condition: {cond[0]}, Switch: {cond[1]} - "
                        + ", ".join(str(p) for p in dups)
                        for cond, dups in duplicated
                    )
                )
            if not sorted_by_cond and not column_spec.mapped_from:
                inputs_to = [
                    p for p in self.pipeline_builders if column_spec.name in p.inputs
                ]
                if not inputs_to:
                    raise ArcanaDesignError(
                        f"'{column_spec.name}' is neither an input nor output to any pipeline"
                    )
                if column_spec.salience.level <= ColumnSalience.publication.level:
                    raise ArcanaDesignError(
                        f"'{column_spec.name}' is not generated by any pipeline yet its salience "
                        f"is not specified as 'raw' or 'primary'"
                    )

    @pipeline_builders.validator
    def pipeline_builders_validator(self, _, pipeline_builders):
        for pipeline_builder in pipeline_builders:
            if missing_outputs := [
                o for o in pipeline_builder.outputs if o not in self.column_names
            ]:
                raise ArcanaDesignError(
                    f"'{pipeline_builder.name}' pipeline outputs to unknown columns: {missing_outputs}"
                )


RESERVED_NAMES = ("dataset", "menu", "stack")
RESERVED_NAMES += tuple("_" + n for n in RESERVED_NAMES)


class MenuDescriptor:
    def __get__(self, ins, typ):
        if ins is None:
            raise NotImplementedError("Class-based menu calls are not implemented")
        else:
            raise NotImplementedError("Instance-based menu calls are not implemented")


class StackDescriptor:
    def __get__(self, ins, typ):
        if ins is None:
            raise NotImplementedError("Class-based stack calls are not implemented")
        else:
            raise NotImplementedError("Instance-based stack calls are not implemented")


def make_class(decorated_klass: type, space: type) -> type:
    """
    Construct an analysis class and validate all the components fit together

    Parameters
    ----------
    decorated_klass : type
        a class that is decorated with the @analysis decorator
    space : type
        a subclass of the DataSpace enum, which defines the types of
        datasets the analysis can be applied to

    Returns
    -------
    type
        the analysis class
    """

    # Remove this type annotation for now so specs from inherited classes don't
    # get interpreted as an attribute
    try:
        del decorated_klass.__annotations__["__spec__"]
    except KeyError:
        pass

    # Resolve 'Inherited' and 'mapped_from' attributes to a form that `attrs` can
    # recognise so the attributes are created
    for name, tmp_attr in list(decorated_klass.__dict__.items()):
        if name in RESERVED_NAMES:
            raise ArcanaDesignError(
                f"Cannot use reserved name '{name}' for attribute in "
                f"'{decorated_klass.__name__}' analysis class"
            )
        # Get attribute type from type annotations
        dtype = decorated_klass.__annotations__.get(name)
        if ty.get_origin(dtype) is DataColumn:
            dtype = ty.get_args(dtype)[0]
        # Resolve inherited and mapped attributes
        if isinstance(tmp_attr, (_Inherited, _MappedFrom)):
            resolved, resolved_dtype = tmp_attr.resolve(name, decorated_klass)
            if dtype is None:
                # Copy type annotation across to new class if it isn't present
                dtype = resolved_dtype
            elif dtype is not resolved_dtype and not issubclass(dtype, resolved_dtype):
                raise ArcanaDesignError(
                    f"Cannot change format of {name} from {resolved_dtype} to "
                    f"{dtype} as it is not a sub-class"
                )
            tmp_attr.attr_name = name
            tmp_attr.dtype = dtype
            tmp_attr = resolved
        # Convert temporary attributes to attrs.fields
        if isinstance(tmp_attr, _TempAttr):
            # Save annotated type of column in metadata and convert to Column
            if dtype is None:
                raise ArcanaDesignError(
                    f"Type annotation must be provided for '{name}' {tmp_attr.attr_type}"
                )
            # Replace temporary attribute with Attrs attribute (as created by attrs.field)
            # and set type annotation
            tmp_attr.attr_name = name
            tmp_attr.dtype = dtype
            attrs_field = tmp_attr.to_attrs_field()
            setattr(decorated_klass, name, attrs_field)
            decorated_klass.__annotations__[name] = dtype
            if isinstance(tmp_attr, _TempSubanalysisAttr):
                setattr(
                    decorated_klass,
                    f"_{name}_default",
                    attrs_field.default(SubanalysisDefault(name)),
                )

    # Ensure that the class has it's own annotaitons dict so we can modify it without
    # messing up other classes
    decorated_klass.__annotations__ = copy(decorated_klass.__annotations__)
    decorated_klass._dataset = attrs.field(default=None)
    decorated_klass.__annotations__["_dataset"] = Dataset
    # Set built-in methods
    decorated_klass.menu = MenuDescriptor()
    decorated_klass.stack = StackDescriptor()
    # Ensure slot gets created for the __spec__ attr in the class generated by
    # attrs.define, if it doesn't already exist (which will be the case when subclassing
    # another analysis class)
    decorated_klass.__spec__ = None

    # Create class using attrs package, will create attributes for all columns and
    # parameters
    attrs_klass = attrs.define(decorated_klass)

    # Initialise lists to hold all the different components of an analysis
    column_specs = []
    pipeline_builders = []
    parameters = []
    switches = []
    checks = []
    subanalyses = []

    # Loop through all attributes created by attrs.define and create specs for columns,
    # parameters and sub-analyses to be stored in the __spec__ attribute
    for attr in attrs_klass.__attrs_attrs__:
        try:
            attr_type = attr.metadata[ATTR_TYPE]
        except KeyError:
            continue
        if attr.inherited:
            continue  # Inherited attributes will be added at the end
        if attr_type == _TempColumnAttr.attr_type:
            row_freq = attr.metadata["row_frequency"]
            if row_freq is None:
                row_freq = max(space)  # "Leaf" frequency of the data tree
            column_specs.append(
                ColumnSpec(
                    name=attr.name,
                    type=attr.metadata["datatype"],
                    desc=attr.metadata["desc"],
                    row_frequency=row_freq,
                    salience=attr.metadata["salience"],
                    defined_in=attr.metadata.get("defined_in", [attrs_klass]),
                    modified=attr.metadata.get("modified"),
                    mapped_from=attr.metadata.get("mapped_from"),
                )
            )
        elif attr_type == _TempParameterAttr.attr_type:
            parameters.append(
                Parameter(
                    name=attr.name,
                    type=attr.type,
                    default=attr.default,
                    choices=attr.metadata["choices"],
                    lower_bound=attr.metadata["lower_bound"],
                    upper_bound=attr.metadata["upper_bound"],
                    desc=attr.metadata["desc"],
                    salience=attr.metadata["salience"],
                    defined_in=attr.metadata.get("defined_in", [attrs_klass]),
                    modified=attr.metadata.get("modified"),
                    mapped_from=attr.metadata.get("mapped_from"),
                )
            )
        elif attr_type != "subanalysis":
            raise ValueError(f"Unrecognised attrs type '{attr_type}'")

    # Do another loop and collect all the sub-analyses after we have build the
    # column specs and parameters so we can implicitly add any mappings to the
    for attr in attrs_klass.__attrs_attrs__:
        if (
            attr.metadata.get(ATTR_TYPE) == _TempSubanalysisAttr.attr_type
            and not attr.inherited
        ):
            resolved_mappings = []
            for (from_, to) in attr.metadata["mappings"]:
                resolved_mappings.append((from_, to.attr_name))
            # Add in implicit mappings, where a column from the subanalysis has been
            # mapped into the global namespace of the analysis class
            for col_or_param in chain(column_specs, parameters):
                if (
                    col_or_param.mapped_from
                    and col_or_param.mapped_from[0] == attr.name
                ):
                    resolved_mappings.append(
                        (col_or_param.mapped_from[1], col_or_param.name)
                    )
            subanalyses.append(
                SubanalysisSpec(
                    name=attr.name,
                    type=attr.type,
                    desc=attr.metadata["desc"],
                    mappings=tuple(sorted(resolved_mappings)),
                    defined_in=attr.metadata.get("defined_in", [attrs_klass]),
                )
            )

    # Loop through all attributes to pick up decorated methods for pipelines, checks
    # and switches
    for attr in attrs_klass.__dict__.values():
        try:
            attr_anots = attr.__annotations__
        except AttributeError:
            continue

        if PIPELINE_ANNOTATIONS in attr_anots:
            anots = attr_anots[PIPELINE_ANNOTATIONS]
            outputs = tuple(o.attr_name for o in anots["outputs"])
            input_columns, used_parameters = _get_args_automagically(
                column_specs=column_specs, parameters=parameters, method=attr
            )
            unresolved_condition = anots["condition"]
            if unresolved_condition is not None:
                try:
                    condition = unresolved_condition.metadata["attr_name"]
                except AttributeError:
                    condition = unresolved_condition.resolve(
                        decorated_klass,
                        column_specs=column_specs,
                        parameters=parameters,
                    )
            else:
                condition = None
            pipeline_builders.append(
                PipelineBuilder(
                    name=attr.__name__,
                    desc=attr.__doc__,
                    inputs=input_columns,
                    outputs=outputs,
                    parameters=used_parameters,
                    condition=condition,
                    switch=anots["switch"],
                    method=attr,
                    defined_in=attrs_klass,
                )
            )
        elif SWICTH_ANNOTATIONS in attr_anots:
            input_columns, used_parameters = _get_args_automagically(
                column_specs=column_specs, parameters=parameters, method=attr
            )
            switches.append(
                Switch(
                    name=attr.__name__,
                    desc=__doc__,
                    inputs=input_columns,
                    parameters=used_parameters,
                    method=attr,
                    defined_in=attrs_klass,
                )
            )
        elif CHECK_ANNOTATIONS in attr_anots:
            anots = attr_anots[CHECK_ANNOTATIONS]
            column_name = anots["column"].attr_name
            input_columns, used_parameters = _get_args_automagically(
                column_specs=column_specs, parameters=parameters, method=attr
            )
            checks.append(
                Check(
                    name=attr.__name__,
                    column=column_name,
                    desc=attr.__doc__,
                    inputs=input_columns,
                    parameters=used_parameters,
                    salience=anots["salience"],
                    method=attr,
                    defined_in=attrs_klass,
                )
            )

    # Combine with specs from base classes
    for base in attrs_klass.__mro__[1:]:
        if not hasattr(base, "__spec__"):
            continue  # skip classes that aren't decorated analyses
        if base.__spec__.space is not space:
            raise ValueError(
                "Cannot redefine the space that an analysis operates on from "
                f"{base.__spec__.space} to {space}"
            )
        # Append column specs, parameters and subanalyses that were inherited from base
        # classes
        for lst, base_lst in (
            (column_specs, base.__spec__.column_specs),
            (parameters, base.__spec__.parameters),
            (subanalyses, base.__spec__.subanalyses),
        ):
            lst.extend(b for b in base_lst if b.name not in (x.name for x in lst))
            if overwritten := [
                nme
                for nme, atr in decorated_klass.__dict__.items()
                if nme in (b.name for b in base_lst)
                and (not hasattr(atr, "metadata") or "defined_in" not in atr.metadata)
            ]:
                raise ArcanaDesignError(
                    f"{overwritten} columns/parameters/subanalyses attributes in base "
                    f"class {base} were overwritten in {attrs_klass} without using "
                    "explicit 'Inherited' function"
                )

        # Check to see that overriding pipelines don't remove outputs
        for base_builder in base.__spec__.pipeline_builders:
            try:
                builder = next(
                    b for b in pipeline_builders if b.name == base_builder.name
                )
            except StopIteration:
                continue
            if missing_outputs := [
                o for o in base_builder.outputs if o not in builder.outputs
            ]:
                raise ArcanaDesignError(
                    f"{missing_outputs} outputs are missing from '{builder.name}' "
                    "pipeline builder, which were defined by the overridden method "
                    "in {base}. Overriding methods can only add new outputs, not "
                    "remove existing ones"
                )
        # Append pipeline specs, switches and checks that were inherited from base
        # classes
        for lst, base_lst in (
            (pipeline_builders, base.__spec__.pipeline_builders),
            (switches, base.__spec__.switches),
            (checks, base.__spec__.checks),
        ):
            lst.extend(b for b in base_lst if b.name not in (x.name for x in lst))

    attrs_klass.__spec__ = AnalysisSpec(
        space=space,
        column_specs=tuple(sorted(column_specs, key=attrgetter("name"))),
        pipeline_builders=tuple(sorted(pipeline_builders, key=attrgetter("name"))),
        parameters=tuple(sorted(parameters, key=attrgetter("name"))),
        switches=tuple(sorted(switches, key=attrgetter("name"))),
        checks=tuple(sorted(checks, key=attrgetter("name"))),
        subanalyses=tuple(sorted(subanalyses, key=attrgetter("name"))),
    )
    attrs_klass.__annotations__["__spec__"] = AnalysisSpec

    return attrs_klass


@attrs.define
class _Inherited:

    to_overwrite: ty.Dict[str, ty.Any]
    resolved_to: str = None
    attr_name: str = None
    dtype: str = None

    def resolve(self, name, klass):
        """Resolve to columns and parameters in the specified class

        Parameters
        ----------
        name : str
            the name of the attribute in the class to resolve the inherited attribute to
        klass : type
            the initial class to be transformed into an analysis class
        """
        defined_in = []
        for base in klass.__mro__[1:-1]:  # skip current class and base "object" class
            if name in base.__dict__:
                defined_in.append(base)
        if not defined_in:
            raise AttributeError(
                f"Supers of {klass} have no attribute named '{name}' to inherit"
            )
        attr_to_inherit = getattr(attrs.fields(defined_in[0]), name)
        resolved = _attr_to_temp(attr_to_inherit, self.to_overwrite)
        # List the sub-classes where the attribute is used/defined
        resolved.metadata["defined_in"] = defined_in
        resolved.metadata["overridden"] = deepcopy(self.to_overwrite)
        # Return the resolved attribute and its type annotation
        return resolved, resolved.metadata["datatype"]


@attrs.define
class _MappedFrom:

    subanalysis_name: str
    name: str
    to_overwrite: ty.Dict[str, ty.Any]
    resolved_to: str = None
    attr_name: str = None
    dtype: str = None

    def resolve(self, name, klass):
        """Resolve to a column temporary attribute to be transformed into a attribute
        of the analysis class

        Parameters
        ----------
        name : str
            the name of the attribute in the class to resolve the inherited attribute to
        klass : type
            the initial class to be transformed into an analysis class
        """
        analysis_class = klass.__annotations__[self.subanalysis_name]
        # Get the Attribute in the subanalysis class
        try:
            attr_in_sub = next(
                a for a in analysis_class.__attrs_attrs__ if a.name == self.name
            )
        except StopIteration:
            raise ArcanaDesignError(
                f"No attribute named '{self.name}' in subanalysis "
                f"'{self.subanalysis_name}' ({analysis_class}): "
                + str([a.name for a in analysis_class.__attrs_attrs__])
            )

        resolved = _attr_to_temp(attr_in_sub, self.to_overwrite)
        resolved.metadata["mapped_from"] = (self.subanalysis_name, self.name)
        return resolved, attr_in_sub.type


@attrs.define
class _UnresolvedOp:
    """An operation within a conditional expression that hasn't been resolved"""

    operator: str
    operands: ty.Tuple[str]

    def resolve(self, klass, column_specs, parameters):
        """Resolves counting attribute operands to the names of attributes in the class

        Parameters
        ----------
        klass : type
            the class being wrapped by attrs.define
        column_specs : list[ColumnSpec]
            the column specs defined in the class
        parameters : list[Parameter]
            the parameters defined in the class

        Return
        ------
        _Op
            the operator with counting-attributes by attrs resolved to attribute names
        """
        resolved = []
        parameter_names = [p.name for p in parameters]
        column_names = [c.name for c in column_specs]
        for operand in self.operands:
            if isinstance(operand, _UnresolvedOp):
                operand = operand.resolve(klass, column_specs, parameters)
            else:
                try:
                    operand = operand.attr_name
                except AttributeError:
                    pass
            resolved.append(operand)
        if self.operator == "value_of":
            assert len(resolved) == 1
            if resolved[0] not in parameter_names:
                raise ValueError(
                    f"'value_of' can only be used on parameter attributes not '{operand}'"
                )
        elif self.operator == "is_provided":
            assert len(resolved) <= 2
            if resolved[0] not in column_names:
                raise ValueError(
                    f"'is_provided' can only be used on column specs not '{operand}'"
                )
        return Operation(self.operator, tuple(resolved))

    def __eq__(self, o):
        return _UnresolvedOp("eq", (self, o))

    def __ne__(self, o):
        return _UnresolvedOp("ne", (self, o))

    def __lt__(self, o):
        return _UnresolvedOp("lt", (self, o))

    def __le__(self, o):
        return _UnresolvedOp("le", (self, o))

    def __gt__(self, o):
        return _UnresolvedOp("gt", (self, o))

    def __ge__(self, o):
        return _UnresolvedOp("ge", (self, o))

    def __and__(self, o):
        return _UnresolvedOp("and_", (self, o))

    def __or__(self, o):
        return _UnresolvedOp("or_", (self, o))

    def __invert__(self):
        return _UnresolvedOp("invert_", (self,))


def _get_args_automagically(column_specs, parameters, method, index_start=2):
    """Automagically determine inputs to pipeline or switched by matching
    a methods argument names with columns and parameters of the class

    Parameters
    ----------
    column_specs : list[ColumnSpec]
        the column specs to match the inputs against
    parameters : list[Parameter]
        the parameters to match the inputs against
    method : bound-method
        the method to automagically determine the inputs for
    index_start : int
        the argument index to start from (i.e. can be used to skip the workflow
        arg passed to pipelines classes)

    Returns
    -------
    list[str]
        the names of the input columns to automagically provide to the method
    list[str]
        the names of the parameters to automagically provide to the method
    """
    inputs = []
    used_parameters = []
    column_names = [c.name for c in column_specs]
    param_names = [p.name for p in parameters]
    signature = inspect.signature(method)
    for arg in list(signature.parameters)[
        index_start:
    ]:  # First arg is self and second is the workflow object to add to
        required_type = method.__annotations__.get(arg)
        if arg in column_names:
            column_spec = next(c for c in column_specs if c.name == arg)
            if required_type is not None and required_type is not column_spec.type:
                # Check to see whether conversion is possible
                required_type.find_converter(column_spec.type)
            inputs.append(arg)
        elif arg in param_names:
            used_parameters.append(arg)
        else:
            raise ArcanaDesignError(
                f"Unrecognised argument '{arg}'. If it is from a base class, "
                "make sure that it is explicitly inherited using the `Inherited` "
                "function."
            )
    return tuple(inputs), tuple(used_parameters)


def _attr_to_temp(attr, to_overwrite):
    """Reverts an Attribute as found in the __attrs_attrs__ of another class back into
    a temporary attribute so it can be used to specify to
    the `attrs` package to make a new attribute in the new class

    Parameters
    ----------
    attr : Attribute
        the attribute to replicate in the new class
    to_overwrite : dict[str, Any]
        a dictionary of attributes to overwrite when adding the new attribute
    """
    return ATTR_TYPE_TO_TEMP_TYPE[attr.metadata[ATTR_TYPE]].from_attrs_attr(
        attr, to_overwrite
    )


# def _attr_name(cls, counting_attr):
#     """Get the name of a counting attribute by reading the original class dict"""
#     if isinstance(counting_attr, (_Inherited, _MappedFrom)):
#         assert counting_attr.resolved_to is not None
#         return counting_attr.resolved_to
#     try:
#         return next(n for n, v in cls.__dict__.items() if v is counting_attr)
#     except StopIteration:
#         raise AttributeError(f"Attribute {counting_attr} not found in cls {cls}")


class _TempAttr(metaclass=ABCMeta):
    @classmethod
    def from_attrs_attr(cls, attr, to_overwrite, **kwargs):
        metadata = dict(attr.metadata)
        del metadata[ATTR_TYPE]
        kwargs.update(
            {
                a.name: metadata.pop(a.name)
                for a in list(attrs.fields(cls))
                if a.name not in ("metadata", "default", "attr_name", "dtype")
            }
        )
        kwargs.update(to_overwrite)
        tmp_attr = cls(metadata=metadata, **kwargs)
        tmp_attr.metadata["modified"] = tuple(to_overwrite.items())
        return tmp_attr


@attrs.define
class _TempColumnAttr(_TempAttr):

    attr_type = "column"

    desc: str
    row_frequency: DataSpace
    salience: ColumnSalience = ColumnSalience.supplementary
    metadata: dict = attrs.field(factory=dict, converter=lambda m: m if m else {})
    attr_name: str = None
    dtype: type = None

    def to_attrs_field(self):
        assert self.attr_name is not None
        assert self.dtype is not None
        metadata = dict(self.metadata)
        metadata.update(
            {
                ATTR_TYPE: self.attr_type,
                "attr_name": self.attr_name,
                "desc": self.desc,
                "row_frequency": self.row_frequency,
                "salience": self.salience,
                "datatype": self.dtype,
            }
        )
        return attrs.field(
            default=None,
            metadata=metadata,
        )

    # @classmethod
    # def from_attrs_attr(cls, attr, to_overwrite):
    #     metadata = dict(attr.metadata)
    #     del metadata[ATTR_TYPE]
    #     kwargs = {
    #         a: metadata.pop(a.name) for a in list(attrs.fields(cls)) if a != "metadata"
    #     }
    #     kwargs.update(to_overwrite)
    #     return cls(metadata, **kwargs)


@attrs.define
class _TempParameterAttr(_TempAttr):

    attr_type = "parameter"

    desc: str
    default: ty.Any = None
    salience: ParameterSalience = ParameterSalience.recommended
    choices: ty.Union[list, None] = attrs.field(default=None)
    lower_bound: ty.Union[float, int] = None
    upper_bound: ty.Union[float, int] = None
    metadata: dict = attrs.field(factory=dict, converter=lambda m: m if m else {})
    attr_name: str = None
    dtype: type = None

    @choices.validator
    def choices_validator(self, _, choices):
        if choices is not None:
            if self.upper_bound is not None or self.lower_bound is not None:
                raise ArcanaDesignError(
                    f"Cannot specify lower ({self.lower_bound}) or upper "
                    f"({self.upper_bound}) bound in conjunction with 'choices' arg "
                    f"({choices})"
                )

    def to_attrs_field(self):
        assert self.attr_name is not None
        assert self.dtype is not None
        metadata = dict(self.metadata)
        metadata.update(
            {
                ATTR_TYPE: self.attr_type,
                "attr_name": self.attr_name,
                "desc": self.desc,
                "salience": self.salience,
                "choices": self.choices,
                "lower_bound": self.lower_bound,
                "upper_bound": self.upper_bound,
                "datatype": self.dtype,
            }
        )
        return attrs.field(
            default=self.default,
            validator=_parameter_validator,
            metadata=metadata,
        )

    @classmethod
    def from_attrs_attr(cls, attr, to_overwrite, **kwargs):
        return super().from_attrs_attr(
            attr, to_overwrite, default=attr.default, **kwargs
        )


@attrs.define
class _TempSubanalysisAttr(_TempAttr):

    attr_type = "subanalysis"

    desc: str
    mappings: ty.Tuple[ty.Union[str, tuple]]
    metadata: dict = attrs.field(factory=dict, converter=lambda m: m if m else {})
    attr_name: str = None
    dtype: type = None

    def to_attrs_field(self):
        assert self.attr_name is not None
        assert self.dtype is not None
        return attrs.field(
            metadata={
                ATTR_TYPE: self.attr_type,
                "attr_name": self.attr_name,
                "desc": self.desc,
                "mappings": self.mappings,
                "type": self.dtype,
            },
            init=False,
        )

    @classmethod
    def from_attrs_attr(cls, attr, to_overwrite):
        metadata = copy(attr.metadata)
        del metadata[ATTR_TYPE]
        kwargs = {
            a: metadata.pop(a) for a in list(attrs.fields(cls)) if a != "metadata"
        }
        kwargs.update(to_overwrite)
        return cls(metadata, **kwargs)


ATTR_TYPE_TO_TEMP_TYPE = {
    t.attr_type: t for t in (_TempColumnAttr, _TempParameterAttr, _TempSubanalysisAttr)
}


def _parameter_validator(_, attr, val):
    if attr.metadata["choices"] is not None:
        choices = attr.metadata["choices"]
        if val not in choices:
            raise ValueError(
                f"{val} is not a valid value for '{attr.name}' parameter: {choices}"
            )
    else:
        lower_bound = attr.metadata.get("lower_bound")
        upper_bound = attr.metadata.get("upper_bound")
        if not (
            (lower_bound is None or val >= lower_bound)
            and (upper_bound is None or val <= upper_bound)
        ):
            raise ValueError(
                f"Value of '{attr.name}' ({val}) is not within the specified bounds: "
                f"{lower_bound} - {upper_bound}"
            )

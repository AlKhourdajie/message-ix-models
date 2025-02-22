"""Utilities for handling objects from :mod:`sdmx`."""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import datetime
from enum import Enum, Flag
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Union, cast
from warnings import warn

import sdmx
import sdmx.message
from iam_units import registry
from sdmx.model import common

from .common import package_data_path

if TYPE_CHECKING:
    from os import PathLike
    from typing import TypeVar

    from sdmx.message import StructureMessage

    # TODO Use "from typing import Self" once Python 3.11 is the minimum supported
    Self = TypeVar("Self", bound="AnnotationsMixIn")

log = logging.getLogger(__name__)

CodeLike = Union[str, common.Code]


@dataclass
class AnnotationsMixIn:
    """Mix-in for dataclasses to allow (de)serializing as SDMX annotations."""

    # TODO Type with overrides: list → list
    def get_annotations(self, _rtype: Union[type[list], type[dict]]):
        """Return a collection of :class:`.Annotation` for the fields of the object.

        Returns
        -------
        list of :class:`Annotation <sdmx.model.common.Annotation>`
            if `_rtype` is :class:`list`.
        dict
            if `_rtype` is :class:`dict`. The dict has the one key "annotations", mapped
            to a :class:`list` of Annotations. This can be used as a keyword argument
            to the constructor of a :class:`.AnnotableArtefact` subclass.
        """
        result = []
        for f in fields(self):
            anno_id = f.name.replace("_", "-")
            result.append(
                common.Annotation(id=anno_id, text=repr(getattr(self, f.name)))
            )

        if _rtype is list:
            return result
        else:
            return dict(annotations=result)

    @classmethod
    def from_obj(cls: type["Self"], obj: common.AnnotableArtefact) -> "Self":
        """Return a new instance of `cls` given an AnnotableArtefact `obj`."""
        args = []
        for f in fields(cls):
            anno_id = f.name.replace("_", "-")
            args.append(obj.eval_annotation(id=anno_id))

        return cls(*args)


# FIXME Reduce complexity from 13 → ≤11
def as_codes(  # noqa: C901
    data: Union[list[str], dict[str, CodeLike]],
) -> list[common.Code]:
    """Convert `data` to a :class:`list` of :class:`.Code` objects.

    Various inputs are accepted:

    - :class:`list` of :class:`str`.
    - :class:`dict`, in which keys are :attr:`~sdmx.model.common.Code.id` and values are
      further :class:`dict` with keys matching other Code attributes.
    """
    # Assemble results as a dictionary
    result: dict[str, common.Code] = {}

    if isinstance(data, list):
        # FIXME typing ignored temporarily for PR#9
        data = dict(zip(data, data))  # type: ignore [arg-type]
    elif not isinstance(data, Mapping):
        raise TypeError(data)

    for id, info in data.items():
        # Pass through Code; convert other types to dict()
        if isinstance(info, common.Code):
            result[info.id] = info
            continue
        elif isinstance(info, str):
            _info = dict(name=info)
        elif isinstance(info, Mapping):
            _info = dict(info)
        else:
            raise TypeError(info)

        # Create a Code object
        code = common.Code(
            id=str(id),
            name=_info.pop("name", str(id).title()),
        )

        # Store the description, if any
        try:
            code.description = common.InternationalString(
                value=_info.pop("description")
            )
        except KeyError:
            pass

        # Associate with a parent
        try:
            parent_id = _info.pop("parent")
        except KeyError:
            pass  # No parent
        else:
            result[parent_id].append_child(code)

        # Associate with any children
        for id in _info.pop("child", []):
            try:
                code.append_child(result[id])
            except KeyError:
                pass  # Not parsed yet

        # Convert other dictionary (key, value) pairs to annotations
        for id, value in _info.items():
            code.annotations.append(
                common.Annotation(
                    id=id, text=value if isinstance(value, str) else repr(value)
                )
            )

        result[code.id] = code

    return list(result.values())


def eval_anno(obj: common.AnnotableArtefact, id: str):
    """Retrieve the annotation `id` from `obj`, run :func:`eval` on its contents.

    .. deprecated:: 2023.9.12

       Use :meth:`sdmx.model.common.AnnotableArtefact.eval_annotation`, which provides
       the same functionality.
    """
    warn(
        "message_ix_models.util.eval_anno; use sdmx.model.common.AnnotableArtefact"
        ".eval_annotation() instead.",
        DeprecationWarning,
        stacklevel=2,
    )

    try:
        value = str(obj.get_annotation(id=id).text)
    except KeyError:  # No such attribute
        return None

    try:
        return eval(value, {"registry": registry})
    except Exception as e:  # Something that can't be eval()'d, e.g. a plain string
        log.debug(f"Could not eval({value!r}): {e}")
        return value


class URNLookupEnum(Enum):
    """:class:`.Enum` subclass that allows looking up members using a URN."""

    _ignore_ = "_urn_name"
    _urn_name: dict

    def __init_subclass__(cls):
        cls._urn_name = dict()

    @classmethod
    def by_urn(cls, urn: str):
        """Return the :class:`.Enum` member given its `urn`."""
        return cls[cls.__dict__["_urn_name"][urn]]


def make_enum(urn, base=URNLookupEnum):
    """Create an :class:`.enum.Enum` (or `base`) with members from codelist `urn`."""
    # Read the code list
    cl = read(urn)

    # Ensure the 0 member is NONE, not any of the codes
    names = ["NONE"] if issubclass(base, Flag) else []
    names.extend(code.id for code in cl)

    # Create the class
    result = base(urn, names)

    if issubclass(base, URNLookupEnum):
        # Populate the URN → member name mapping
        for code in cl:
            result._urn_name[code.urn] = code.id

    return result


def read(urn: str, base_dir: Optional["PathLike"] = None):
    """Read SDMX object from package data given its `urn`."""
    # Identify a path that matches `urn`
    base_dir = Path(base_dir or package_data_path("sdmx"))
    urn = urn.replace(":", "_")  # ":" invalid on Windows
    paths = sorted(
        set(base_dir.glob(f"*{urn}*.xml")) | set(base_dir.glob(f"*{urn.upper()}*.xml"))
    )

    if len(paths) > 1:
        log.info(
            f"Match {paths[0].relative_to(base_dir)} for {urn!r}; {len(paths) - 1} "
            "other result(s)"
        )

    try:
        with open(paths[0], "rb") as f:
            msg = cast("StructureMessage", sdmx.read_sdmx(f))
    except IndexError:
        raise FileNotFoundError(f"'*{urn}*.xml', '*{urn.upper()}*.xml' or similar")

    for _, cls in msg.iter_collections():
        try:
            return next(iter(msg.objects(cls).values()))
        except StopIteration:
            pass


def write(obj, base_dir: Optional["PathLike"] = None, basename: Optional[str] = None):
    """Store an SDMX object as package data."""
    base_dir = Path(base_dir or package_data_path("sdmx"))

    if isinstance(obj, sdmx.message.StructureMessage):
        msg = obj
        assert basename
    else:
        # Set the URN of the object
        obj.urn = sdmx.urn.make(obj)

        # Wrap the object in a StructureMessage
        msg = sdmx.message.StructureMessage()
        msg.add(obj)

        # Identify a path to write the file. ":" is invalid on Windows.
        basename = basename or obj.urn.split("=")[-1].replace(":", "_")

    msg.header = sdmx.message.Header(
        source=f"Generated by message_ix_models {version('message_ix_models')}",
        prepared=datetime.now(),
    )

    path = base_dir.joinpath(f"{basename}.xml")

    # Write
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(sdmx.to_xml(msg, pretty_print=True))

    log.info(f"Wrote {path}")


def register_agency(agency: "common.Agency") -> "common.AgencyScheme":
    """Add `agency` to the :class:`.AgencyScheme` "IIASA_ECE:AGENCIES"."""
    # Read the existing agency scheme
    as_ = read("IIASA_ECE:AGENCIES")

    if agency in as_:
        log.info(f"Replace or update existing {as_[agency.id]!r}")
        as_.items[agency.id] = agency
    else:
        as_.append(agency)

    log.info(f"Updated {as_!r}")

    # Write to file again
    write(as_)

    return as_

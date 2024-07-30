import logging
from copy import deepcopy

import pandas as pd
import xarray as xr

from ixmp.reporting import Quantity

from message_data.tools import set_info
from message_data.tools.gea import SUPPORTED as GEA_SUPPORTED, get_gea_data
from message_data.model.transport.utils import consumer_groups

log = logging.getLogger(__name__)


# Query for retrieving GEA population data

GEA_DIMS = dict(
    variable={
        "Population|Total": "total",
        "Population|Urban": "UR+SU",
        "Population|Rural": "RU",
    },
    scenario={
        "geama_450_btr_full": "GEA mix",
        "geaha_450_atr_full": "GEA supply",
        "geala_450_atr_nonuc": "GEA eff",
    },
    region={},
)


def get_consumer_groups(context):
    """Return shares of transport consumer groups.

    Parameters
    ----------
    context : .Context
        The ``.regions`` attribute is passed to :func:`get_urban_rural_shares`.

    Returns
    -------
    ixmp.reporting.Quantity
        Dimensions: n, y, cg.
    """
    cg_indexers = deepcopy(consumer_groups(rtype="indexers"))
    consumer_group = cg_indexers.pop("consumer_group")

    # Data: GEA population projections give split between 'UR+SU' and 'RU'
    ursu_ru = get_urban_rural_shares(context)

    # Assumption: split of population between area_type 'UR' and 'SU'
    # - Fill forward along years, for nodes where only a year 2010 value is
    #   assumed.
    # - Fill backward 2010 to 2005, in order to compute
    su_share = (
        context.data["transport population-suburb-share"].ffill("year").bfill("year")
    )

    # Assumption: global nodes are assumed to match certain U.S.
    # census_divisions
    n_cd_map = context["transport config"]["node to census_division"]
    n, cd = zip(*n_cd_map.items())
    n_cd_indexers = dict(
        node=xr.DataArray(list(n), dims="node"),
        census_division=xr.DataArray(list(cd), dims="node"),
    )

    # Split the GEA 'UR+SU' population share using su_share
    pop_share = xr.concat(
        [
            ursu_ru.sel(area_type="UR+SU", drop=True) * (1 - su_share),
            ursu_ru.sel(area_type="UR+SU", drop=True) * su_share,
            ursu_ru.sel(area_type="RU", drop=True),
        ],
        dim=pd.Index(["UR", "SU", "RU"], name="area_type"),
    )

    # Index of pop_share versus the previous period
    pop_share_index = pop_share / pop_share.shift(year=1)

    # DLM: “Values from MA3T are based on 2001 NHTS survey and some more recent
    # calculations done in 2008 timeframe. Therefore, I assume that the numbers
    # here are applicable to the US in 2005.”
    # NB in the spreadsheet, the data are also filled forward to 2010
    ma3t_pop = context.data["transport ma3t population"].assign_coords(year=2010)

    # - Apply the trajectory of pop_share to the initial values of ma3t_pop.
    # - Compute the group shares.
    # - Select using matched sequences, i.e. select a sequence of (node,
    #   census_division) coordinates.
    # - Drop the census_division.
    # - Collapse area_type, attitude, driver_type dimensions into
    #   consumer_group.
    # - Convert to short dimension names.
    groups = (
        (
            ma3t_pop
            * pop_share_index.cumprod("year")
            * context.data["transport ma3t attitude"]
            * context.data["transport ma3t driver"]
        )
        .sel(**n_cd_indexers)
        .drop_vars("census_division")
        .sel(**cg_indexers)
        .drop_vars(cg_indexers.keys())
        .assign_coords(consumer_group=consumer_group)
        .rename(node="n", year="y", consumer_group="cg")
    )

    # Normalize so the sum across groups is always 1; convert to Quantity
    return Quantity(groups / groups.sum("cg"))


def get_urban_rural_shares(context) -> xr.DataArray:
    """Return shares of urban and rural population from GEA.

    Parameters
    ----------
    context : .Context
        The ``.regions`` attribute determines the regional aggregation used.

    See also
    --------
    .get_gea_population
    """
    if context.regions not in GEA_SUPPORTED["regions"]:
        raise NotImplementedError(
            f"GEA population data only available for {repr(GEA_SUPPORTED['regions'])};"
            f" got {repr(context.regions)}"
        )

    # Retrieve region info for the selected regional aggregation
    nodes = set_info(f"node/{context.regions}")
    # List of regions according to the context
    regions = nodes[nodes.index("World")].child
    pop = get_gea_population(regions)

    # Scenario to use, e.g. "GEA mix"
    pop_scen = context["transport config"]["data source"]["population"]

    # Compute shares, select the appropriate scenario
    return (pop.sel(area_type=["UR+SU", "RU"]) / pop.sel(area_type="total")).sel(
        scenario=pop_scen, drop=True
    )


def get_gea_population(regions=[]):
    """Load population data from the GEA database.

    Parameters
    ----------
    regions : list of str
        Regions for which to return population. Prefixes before and including "_" are
        stripped, e.g. "R11_AFR" results in a query for "AFR".

    See also
    --------
    .get_gea_data
    """
    # Identify the regions to query from the GEA data, which has R5 and other mappings
    GEA_DIMS["region"].update({r.split("_")[-1]: r for r in map(str, regions)})

    # Assemble query string and retrieve data from GEA snapshot
    pop = get_gea_data(
        " and ".join(
            f"{dim} in {list(values.keys())}" for dim, values in GEA_DIMS.items()
        )
    )

    # Rename values along dimensions
    for dim, values in GEA_DIMS.items():
        pop = pop.rename(values, level=dim)

    # - Remove model, units dimensions
    # - Rename 'variable' to 'area_type'
    # - Convert to xarray
    return xr.DataArray.from_series(
        pop.droplevel(["model", "unit"]).rename_axis(
            index={"variable": "area_type", "region": "node"}
        )
    )

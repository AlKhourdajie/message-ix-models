"""Generate input data."""
from collections import defaultdict
import logging

import pandas as pd

from message_data.tools import (
    ScenarioInfo,
    add_par_data,
    broadcast,
    get_context,
    make_df,
    make_io,
    make_matched_dfs,
    make_source_tech,
    same_node,
)
from message_data.model.transport.utils import add_commodity_and_level
from .groups import get_consumer_groups  # noqa: F401
from .ldv import get_ldv_data
from .non_ldv import get_non_ldv_data


log = logging.getLogger(__name__)

DATA_FUNCTIONS = [
    get_ldv_data,
    get_non_ldv_data,
]


def add_data(scenario, context, dry_run=False):
    """Populate `scenario` with MESSAGE-Transport data."""
    # Information about `scenario`
    info = ScenarioInfo(scenario)
    context["transport build info"] = info

    # Check for two "node" values for global data, e.g. in
    # ixmp://ene-ixmp/CD_Links_SSP2_v2.1_clean/baseline
    if {"World", "R11_GLB"} < set(info.set["node"]):
        log.warning("Remove 'R11_GLB' from node list for data generation")
        info.set["node"].remove("R11_GLB")

    for func in DATA_FUNCTIONS:
        # Generate or load the data; add to the Scenario
        log.info(f"from {func.__name__}()")
        add_par_data(scenario, func(context), dry_run=dry_run)

    log.info("done")


def demand(context):
    """Return transport demands.

    Parameters
    ----------
    info : .ScenarioInfo
    """
    from message_data.model.transport.demand import dummy, from_external_data

    config = context["transport config"]["data source"]

    if config.get("demand dummy", False):
        return dict(demand=dummy())

    # Retrieve a Reporter configured do to the calculation for the input data
    rep = from_external_data(context["transport build info"], context)

    # Generate the demand data; convert to pd.DataFrame
    data = rep.get("transport pdt:n-y-t").to_series().reset_index(name="value")

    common = dict(
        level="useful",
        time="year",
        unit="km",
    )

    # Convert to message_ix layout
    # TODO combine the two below in a loop or push the logic to demand.py
    data = make_df(
        "demand",
        node=data["n"],
        commodity="transport pax " + data["t"].str.lower(),
        year=data["y"],
        value=data["value"],
        **common,
    )
    data = data[~data["commodity"].str.contains("ldv")]

    data2 = rep.get("transport ldv pdt:n-y-cg").to_series().reset_index(name="value")

    data2 = make_df(
        "demand",
        node=data2["n"],
        commodity="transport pax " + data2["cg"],
        year=data2["y"],
        value=data2["value"],
        **common,
    )

    # result = dict(demand=pd.concat([data, data2]))
    result = dict(demand=data2)

    # commented: for debugging
    # result["demand"].to_csv("debug.csv")

    return result


DATA_FUNCTIONS.append(demand)


def conversion(context):
    """Input and output data for conversion technologies:

    The technologies are named 'transport {mode} load factor'.
    """
    cfg = context["transport config"]
    info = context["transport build info"]

    common = dict(
        year_vtg=info.Y,
        year_act=info.Y,
        mode="all",
        # No subannual detail
        time="year",
        time_origin="year",
        time_dest="year",
    )

    mode_info = [
        ("freight", cfg["factor"]["freight load"], "t km"),
        ("pax", 1.0, "km"),
    ]

    data = defaultdict(list)
    for mode, factor, output_unit in mode_info:
        i_o = make_io(
            (f"transport {mode} vehicle", "useful", "km"),
            (f"transport {mode}", "useful", output_unit),
            factor,
            on="output",
            technology=f"transport {mode} load factor",
            **common,
        )
        for par, df in i_o.items():
            data[par].append(df.pipe(broadcast, node_loc=info.N[1:]).pipe(same_node))

    data = {par: pd.concat(dfs) for par, dfs in data.items()}

    data.update(
        make_matched_dfs(
            base=data["input"],
            capacity_factor=1,
            technical_lifetime=10,
        )
    )

    return data


DATA_FUNCTIONS.append(conversion)


def freight(context):
    """Data for freight technologies."""
    codes = context["transport set"]["technology"]["add"]
    freight_truck = codes[codes.index("freight truck")]
    info = context["transport build info"]

    common = dict(
        year_vtg=info.Y,
        year_act=info.Y,
        mode="all",
        time="year",  # no subannual detail
        time_dest="year",
        time_origin="year",
    )

    data = defaultdict(list)
    for tech in freight_truck.child:
        i_o = make_io(
            src=(None, None, "GWa"),
            dest=("transport freight vehicle", "useful", "km"),
            efficiency=1.0,
            on="input",
            technology=tech.id,
            **common,
        )

        i_o["input"] = add_commodity_and_level(i_o["input"], "final")

        for par, df in i_o.items():
            data[par].append(df.pipe(broadcast, node_loc=info.N[1:]).pipe(same_node))

    data = {par: pd.concat(dfs) for par, dfs in data.items()}

    data.update(
        make_matched_dfs(
            base=data["input"],
            capacity_factor=1,
            technical_lifetime=10,
        )
    )

    return data


DATA_FUNCTIONS.append(freight)


def dummy_supply(context):
    """Dummy fuel supply for the bare RES."""
    return make_source_tech(
        context["transport build info"],
        common=dict(
            commodity="lightoil",
            level="final",
            mode="all",
            technology="DUMMY transport fuel",
            time="year",
            time_dest="year",
            unit="GWa",
        ),
        output=1.0,
        var_cost=1.0,
        technical_lifetime=1.0,
    )


DATA_FUNCTIONS.append(dummy_supply)

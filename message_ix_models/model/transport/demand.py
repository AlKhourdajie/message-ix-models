import logging
from functools import partial
from pathlib import Path

import message_ix
import numpy as np
import pandas as pd
import xarray as xr
from dask.core import quote
from genno import Computer, KeyExistsError, computations
from iam_units import registry
from ixmp.reporting import RENAME_DIMS
from message_ix.reporting import Quantity, Reporter

from message_data.tools import Context, ScenarioInfo, broadcast, gea, make_df

from .build import generate_set_elements
from .data.groups import get_consumer_groups, get_gea_population

log = logging.getLogger(__name__)


def dummy(info):
    """Dummy demands.

    Parameters
    ----------
    info : .ScenarioInfo
    """
    common = dict(
        year=info.Y,
        value=10 + np.arange(len(info.Y)),
        level="useful",
        time="year",
    )

    dfs = []

    for commodity in filter(
        lambda c: c.anno.get("demand", False),
        generate_set_elements("commodity"),
    ):
        unit = "t km" if "freight" in commodity.id else "km"
        dfs.append(
            make_df(
                "demand",
                commodity=commodity.id,
                unit=unit,
                **common,
            )
        )

    # # Dummy demand for light oil
    # common['level'] = 'final'
    # dfs.append(
    #     make_df('demand', commodity='lightoil', **common)
    # )

    return pd.concat(dfs).pipe(broadcast, node=info.N[1:])


def from_scenario(scenario: message_ix.Scenario) -> Reporter:
    """Return a Reporter for calculating demand based on `scenario`.

    Parameters
    ----------
    Scenario
        Solved Scenario

    Returns
    -------
    Reporter
    """
    rep = Reporter.from_scenario(scenario)

    prepare_reporter(rep, Context.get_instance())

    return rep


def from_external_data(info: ScenarioInfo, context: Context) -> Computer:
    """Return a Reporter for calculating demand from external data."""
    c = Computer()
    prepare_reporter(c, context, exogenous_data=True, info=info)

    return c


def qty_from_context(context: Context, ctx_key, **kwargs):
    """Add context data (i.e. files) to the reporter."""
    # Rename long column names ("node") to short ("n")
    qty = Quantity(context.data[ctx_key], **kwargs)
    return qty.rename({k: v for k, v in RENAME_DIMS.items() if k in qty.coords})


def add_exogenous_data(c: Computer, context: Context):
    """Add data to `c` that mocks data coming from an actual Scenario."""
    # Commodity prices: all equal to 1
    # TODO add external data source
    context.data["transport PRICE_COMMODITY"] = 0.1 * xr.ones_like(
        context.data["transport gdp"]
    ).expand_dims({"c": ["transport"]})

    def _add(key, ctx_key, **kwargs):
        c.add(key, qty_from_context(context, ctx_key, **kwargs), index=True, sums=True)

    _add("GDP:n-y", "transport gdp", units="GUSD / year")
    _add("MERtoPPP:n-y", "transport mer-to-ppp")
    _add("PRICE_COMMODITY:n-c-y", "transport PRICE_COMMODITY", units="USD / km")


def add_structure(c: Computer, info: ScenarioInfo):
    """Add keys to `c` for model structure required by demand computations.

    This uses `info` to mock the contents that would be reported from an already-
    populated Scenario for sets "node", "year", and "cat_year".
    """
    for key, value in (
        ("n", quote(list(map(str, info.set["node"])))),
        ("y", quote(info.set["year"])),
        (
            "cat_year",
            pd.DataFrame([["firstmodelyear", info.y0]], columns=["type_year", "year"]),
        ),
    ):
        try:
            # strict=True to raise an exception if `key` exists
            c.add_single(key, value, strict=True)
        except KeyExistsError:
            # Already present; don't overwrite
            continue


def prepare_reporter(
    rep: Computer,
    context: Context,
    configure: bool = True,
    exogenous_data: bool = False,
    info: ScenarioInfo = None,
) -> None:
    """Prepare `rep` for calculating transport demand.

    Parameters
    ----------
    rep : Reporter
        Must contain the keys ``<GDP:n-y>``, ``<MERtoPPP:n-y>``.
    """
    # Ensure the current settings are supported
    gea.supports(context)

    if configure:
        # Configure the reporter; keys are stored
        rep.configure(transport=context["transport config"])

    add_structure(rep, info)

    if exogenous_data:
        add_exogenous_data(rep, context)

    rep.graph["config"].update({"output_path": context.get("output_path", Path.cwd())})

    # Existing keys, prepared by from_scenario() or from_external_data()
    gdp = rep.full_key("GDP")
    mer_to_ppp = rep.full_key("MERtoPPP")
    price_full = rep.full_key("PRICE_COMMODITY").drop("h", "l")

    # Values based on configuration
    rep.add("speed:t", speed, "config")
    rep.add("whour:", whour, "config")
    rep.add("lambda:", _lambda, "config")

    # List of nodes excluding "World"
    # TODO move upstream to message_ix
    rep.add("n:ex world", nodes_ex_world, "n")

    # List of model years
    rep.add("y:model", model_periods, "y", "cat_year")

    # Base share data
    rep.add("base shares:n-t-y", base_shares, "n:ex world", "y", "config")

    # Population data from GEA
    pop_key = rep.add("population:n-y", population, "n:ex world", "config")

    # Consumer group sizes
    # TODO ixmp is picky here when there is no separate argument to the callable; fix.
    cg_key = rep.add("cg share:n-y-cg", get_consumer_groups, context)

    # PPP GDP, total and per capita
    gdp_ppp = rep.add("product", "GDP:n-y:PPP", gdp, mer_to_ppp)
    gdp_ppp_cap = rep.add("ratio", "GDP:n-y:PPP+percapita", gdp_ppp, pop_key)

    # Total demand
    rep.add("transport pdt:n-y:total", total_pdt, gdp_ppp_cap, "config")

    # Value-of-time multiplier
    rep.add("votm:n-y", votm, gdp_ppp_cap)

    # Select only the price of transport services
    price_sel = rep.add(
        price_full.add_tag("transport"),
        rep.get_comp("select"),
        price_full,
        # TODO should be the full set of prices
        dict(c="transport"),
    )
    # Smooth prices to avoid zig-zag in share projections
    price = rep.add(price_sel.add_tag("smooth"), smooth, price_sel)

    # Transport costs by mode
    rep.add(
        "cost:n-y-c-t",
        cost,
        price,
        gdp_ppp_cap,
        "whour:",
        "speed:t",
        "votm:n-y",
        "y:model",
    )

    # Share weights
    rep.add(
        "share weight:n-t-y",
        share_weight,
        "base shares:n-t-y",
        gdp_ppp_cap,
        "cost:n-y-c-t",
        "n:ex world",
        "y:model",
        "t:transport",
        "cat_year",
        "config",
    )

    # Shares
    rep.add(
        "shares:n-t-y",
        partial(logit, dim="t"),
        "cost:n-y-c-t",
        "share weight:n-t-y",
        "lambda:",
        "y:model",
    )

    # Total PDT shared out by mode
    rep.add("product", "transport pdt", "transport pdt:n-y:total", "shares:n-t-y")

    # LDV PDT shared out by mode
    rep.add(
        "select", "transport ldv pdt:n-y:total", "transport pdt:n-y-t", dict(t=["LDV"])
    )

    rep.add(
        "product",
        "transport ldv pdt",
        "transport ldv pdt:n-y:total",
        cg_key,
    )


def base_shares(nodes, y, config):
    """Return base mode shares."""
    modes = config["transport"]["demand modes"]
    return Quantity(
        xr.DataArray(1.0 / len(modes), coords=[nodes, y, modes], dims=["n", "y", "t"])
    )


def model_periods(y, cat_year):
    """Return the elements of `y` beyond the firstmodelyear of `cat_year`."""
    return list(
        filter(
            lambda year: cat_year.query("type_year == 'firstmodelyear'")["year"].item()
            <= year,
            y,
        )
    )


def nodes_ex_world(nodes):
    """Nodes excluding 'World'."""
    return list(filter(lambda n_: "GLB" not in n_ and n_ != "World", nodes))


def share_weight(share, gdp_ppp_cap, cost, nodes, y, t, cat_year, config):
    """Calculate mode share weights."""
    # Modes from configuration
    modes = config["transport"]["demand modes"]

    # Lambda, from configuration
    lamda = config["transport"]["lambda"]

    # Selectors
    t0 = dict(t=modes[0])
    y0 = dict(y=y[0])
    yC = dict(y=config["transport"]["year convergence"])
    years = list(filter(lambda year: year <= yC["y"], y))

    # Share weights
    weight = xr.DataArray(coords=[nodes, years, modes], dims=["n", "y", "t"])

    # Weights in y0 for all modes and nodes
    s_y0 = share.sel(y0, t=modes, n=nodes)
    c_y0 = cost.sel(y0, t=modes, n=nodes).sel(c="transport", drop=True)
    tmp = s_y0 / c_y0 ** lamda

    # Normalize against first mode's weight
    # TODO should be able to avoid a cast and align here
    tmp = tmp / tmp.sel(t0, drop=True)
    *_, tmp = xr.align(weight.loc[y0], xr.DataArray.from_series(tmp).sel(y0))
    weight.loc[y0] = tmp

    # Normalize to 1 across modes
    weight.loc[y0] = weight.loc[y0] / weight.loc[y0].sum("t")

    # Weights at the convergence year, yC
    for node in nodes:
        # Set of 1+ nodes to converge towards
        ref_nodes = config["transport"]["share weight convergence"][node]

        # Ratio between this node's GDP and that of the first reference node
        scale = float(
            gdp_ppp_cap.sel(n=node, **yC, drop=True)
            / gdp_ppp_cap.sel(n=ref_nodes[0], **yC, drop=True)
        )

        # Scale weights in yC
        weight.loc[dict(n=node, **yC)] = scale * weight.sel(n=ref_nodes, **y0).mean(
            "n"
        ) + (1 - scale) * weight.sel(n=node, **y0)

    # Currently not enabled
    # “Set 2010 sweight to 2005 value in order not to have rail in 2010, where
    # technologies become available only in 2020”
    # weight.loc[dict(y=2010)] = weight.loc[dict(y=2005)]

    # Interpolate linearly between y0 and yC
    # NB this will not work if yC is before the final period; it will leave NaN
    #    after yC
    weight = weight.interpolate_na(dim="y")

    return Quantity(weight)


def speed(config):
    """Return travel speed [distance / time].

    The returned Quantity has dimension ``t`` (technology).
    """
    # Convert the dict from the YAML file to a Quantity
    data = pd.Series(config["transport"]["speeds"])
    dim = RENAME_DIMS.get(data.pop("_dim"))
    units = data.pop("_unit")
    return Quantity(data.rename_axis(dim), units=units)


def whour(config):
    """Return work duration [hours / person-year]."""
    q = registry(config["transport"]["work hours"])
    return Quantity(q.magnitude, units=q.units)


def _lambda(config):
    """Return lambda parameter for transport mode share equations."""
    return Quantity(config["transport"]["lambda"], units="")


def total_pdt(gdp_ppp_cap, config):
    """Compute total passenger distance traveled (PDT).

    Simplification of Schäefer et al. (2010): linear interpolation between (0, 0) and
    the configuration keys "fixed demand" and "fixed GDP".
    """
    fix_gdp = registry(config["transport"]["fixed GDP"])
    fix_demand = registry(config["transport"]["fixed demand"])

    result = (gdp_ppp_cap / fix_gdp.magnitude) * fix_demand.magnitude

    # Consistent output units
    result.attrs["_unit"] = (
        gdp_ppp_cap.attrs["_unit"] / fix_gdp.units
    ) * fix_demand.units

    return result


def assert_units(qty, exp):
    """Assert that `qty` has units `exp`."""
    assert (
        qty.attrs["_unit"] / qty.attrs["_unit"]._REGISTRY(exp)
    ).dimensionless, f"Units '{qty.attrs['_unit']:~}'; expected {repr(exp)}"


def votm(gdp_ppp_cap):
    """Calculate value of time multiplier.

    A value of 1 means the VoT is equal to the wage rate per hour.

    Parameters
    ----------
    gdp_ppp_cap
        PPP GDP per capita.
    """
    assert_units(gdp_ppp_cap, "kUSD / passenger / year")

    result = Quantity(
        1 / (1 + np.exp((30 - gdp_ppp_cap) / 20)), units=registry.dimensionless
    )

    return result


def population(nodes, config):
    """Return population data from GEA.

    Dimensions: n-y. Units: 10⁶ person/passenger.
    """
    pop_scenario = config["transport"]["data source"]["population"]

    data = (
        get_gea_population(nodes)
        .sel(area_type="total", scenario=pop_scenario, drop=True)
        .rename(node="n", year="y")
    )

    # Duplicate 2100 data for 2110
    # TODO use some kind of ffill operation
    data = xr.concat(
        [data, data.sel(y=2100, drop=False).assign_coords(y=2110)], dim="y"
    )

    return Quantity(data, units="Mpassenger")


def smooth(qty):
    """Smooth `qty` (e.g. PRICE_COMMODITY) in the ``y`` dimension."""
    # Convert to xr.DataArray because genno.AttrSeries lacks a .shift() method.
    # Conversion can be removed once Quantity is SparseDataArray.
    q = xr.DataArray.from_series(qty.to_series())

    y = q.coords["y"]

    # General smoothing
    result = 0.25 * q.shift(y=-1) + 0.5 * q + 0.25 * q.shift(y=1)

    # First period
    weights = xr.DataArray([0.4, 0.4, 0.2], coords=[y[:3]], dims=["y"])
    result.loc[dict(y=y[0])] = (q * weights).sum("y", min_count=1)

    # Final period. “closer to the trend line”
    # NB the inherited R file used a formula equivalent to weights like
    #    [-1/8, 0, 3/8, 3/4]; didn't make much sense.
    weights = xr.DataArray([0.2, 0.2, 0.6], coords=[y[-3:]], dims=["y"])
    result.loc[dict(y=y[-1])] = (q * weights).sum("y", min_count=1)

    # NB conversion can be removed once Quantity is SparseDataArray
    return Quantity(result, units=qty.attrs["_unit"])


def cost(price, gdp_ppp_cap, whours, speeds, votm, y):
    """Calculate cost of transport [money / distance].

    Calculated from two components:

    1. The inherent price of the mode.
    2. Value of time, in turn from:

       1. a value of time multiplier (`votm`),
       2. the wage rate per hour (`gdp_ppp_cap` / `whours`), and
       3. the travel time per unit distance (1 / `speeds`).
    """
    add = computations.add
    product = computations.product
    ratio = computations.ratio

    # NB for some reason, the 'y' dimension of result becomes `float`, rather than
    # `int`, in this step
    result = add(
        price,
        ratio(
            product(gdp_ppp_cap, votm),
            product(speeds, whours),
        ),
    )

    return result.sel(y=y)


def logit(x, k, lamda, y, dim):
    r"""Compute probabilities for a logit random utility model.

    The choice probabilities have the form:

    .. math::

       Pr(i) = \frac{k_j x_j ^{\lambda_j}}
                    {\sum_{\forall i \in D} k_i x_i ^{\lambda_i}}
               \forall j \in D

    …where :math:`D` is the dimension named by the `dim` argument. All other dimensions
    are broadcast automatically.
    """
    # Systematic utility
    u = computations.product(k, computations.pow(x, lamda)).sel(y=y)

    # commented: for debugging
    # u.to_csv("u.csv")

    # Logit probability
    return computations.ratio(u, u.sum(dim))

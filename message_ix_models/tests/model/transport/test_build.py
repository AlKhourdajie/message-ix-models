import pytest
from pytest import mark, param

from message_ix_models import testing
from message_ix_models.model.structure import get_codes

from message_data.model.transport import build, report
from message_data.testing import NIE


@pytest.mark.parametrize("years", ["A", "B"])
@pytest.mark.parametrize(
    "regions_arg, regions_exp",
    [(None, "R14"), ("ISR", "ISR"), ("R11", "R11"), ("R14", "R14")],
)
def test_get_spec(transport_context_f, regions_arg, regions_exp, years):
    ctx = transport_context_f
    if regions_arg:
        # Non-default value
        ctx.regions = regions_arg

    ctx.years = years

    # The spec can be generated
    spec = build.get_spec(ctx)

    # The required elements of the "node" set match the configuration
    nodes = get_codes(f"node/{regions_exp}")
    exp = list(map(str, nodes[nodes.index("World")].child))
    assert spec["require"].set["node"] == exp


@pytest.mark.parametrize(
    "years", ["A", param("B", marks=pytest.mark.skip("Not implemented"))]
)
@pytest.mark.parametrize(
    "regions, ldv, nonldv, solve",
    [
        ("R11", None, None, False),  # 31 s
        param("R11", None, None, True, marks=mark.slow),  # 44 s
        param("R11", "US-TIMES MA3T", "IKARUS", False, marks=mark.slow),  # 43 s
        param("R11", "US-TIMES MA3T", "IKARUS", True, marks=mark.slow),  # 74 s
        # Non-R11 configurations currently fail
        param("R14", None, None, False, marks=NIE),
        param("ISR", None, None, False, marks=NIE),
    ],
)
def test_build_bare_res(
    request, transport_context_f, years, regions, ldv, nonldv, solve
):
    """Test that model.transport.build works on the bare RES, and the model solves."""
    # Pre-load transport config/metadata
    ctx = transport_context_f
    ctx.regions = regions
    ctx.years = years

    # Manually modify some of the configuration per test parameters
    ctx["transport config"]["data source"]["LDV"] = ldv
    ctx["transport config"]["data source"]["non-LDV"] = nonldv

    # Generate the relevant bare RES
    scenario = testing.bare_res(request, ctx)

    # Build succeeds without error
    build.main(ctx, scenario, fast=True)

    if solve:
        scenario.solve(solve_options=dict(lpmethod=4))

        # Use Reporting calculations to check the result
        result = report.check(scenario)
        assert result.all(), f"\n{result}"

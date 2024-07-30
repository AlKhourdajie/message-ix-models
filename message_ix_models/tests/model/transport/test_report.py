import pytest

from message_ix_models.util import private_data_path

from message_data.model.transport.report import callback
from message_data.reporting import prepare_reporter, register
from message_data.testing import NIE

from . import built_transport


def test_register_cb():
    register(callback)


@pytest.mark.parametrize(
    "regions, years, solved",
    (
        pytest.param("R11", "A", False),
        pytest.param("R11", "A", True),
        pytest.param("R14", "A", True, marks=NIE),
        pytest.param("ISR", "A", True, marks=NIE),
    ),
)
def test_report_bare(request, transport_context_f, tmp_path, regions, years, solved):
    """Run MESSAGEix-Transport–specific reporting."""
    register(callback)

    ctx = transport_context_f
    ctx.regions = regions
    ctx.years = years
    ctx["output dir"] = tmp_path

    scenario = built_transport(request, ctx, solved=solved)

    rep, key = prepare_reporter(
        scenario, private_data_path("report", "global.yaml"), "transport all"
    )
    rep.configure(output_dir=tmp_path)

    # Get the catch-all key, including plots etc.
    rep.get(key)

import pandas as pd

from genno import Quantity, computations
from ixmp.reporting import RENAME_DIMS
from message_ix_models.util import private_data_path

from message_data.tools import ScenarioInfo


def load_transport_file(basename: str, units=None, name: str = None) -> Quantity:
    """Load transport calibration data from a CSV file.

    Wrapper around :func:`genno.computations.load_file`.

    Parameters
    ----------
    basename : str
        Base name of the file, excluding the :file:`.csv` suffix and the path. The
        full path is constructed automatically using :func:`.private_data_path`.
    units : str or pint.Unit, optional
        Units to assign the the resulting
    name : str, optional
        Name to assign.
    """

    return computations.load_file(
        path=private_data_path("transport", basename).with_suffix(".csv"),
        dims=RENAME_DIMS,
        units=units,
        name=name,
    )


def transport_check(scenario, ACT):
    """Reporting computation for :func:`check`.

    Imported into :mod:`.reporting.computations`.
    """
    info = ScenarioInfo(scenario)

    # Mapping from check name → bool
    checks = {}

    # Correct number of outputs
    ACT_lf = ACT.sel(t=["transport freight load factor", "transport pax load factor"])
    checks["'transport * load factor' technologies are active"] = len(
        ACT_lf
    ) == 2 * len(info.Y) * (len(info.N) - 1)

    # # Force the check to fail
    # checks['(fail for debugging)'] = False

    return pd.Series(checks)

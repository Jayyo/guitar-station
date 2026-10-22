import pytest

from guitarstation import cli


def test_unknown_command_is_an_error():
    with pytest.raises(SystemExit) as e:
        cli.main(["bogus"])
    assert e.value.code == 2


@pytest.mark.parametrize("bad", ["0", "-3", "nan", "inf", "7200"])
def test_seconds_must_be_sane(bad):
    with pytest.raises(SystemExit):
        cli.main(["level", bad])

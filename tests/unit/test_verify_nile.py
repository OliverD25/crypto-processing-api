"""The parts of the Nile verification script that must not need a terminal.

The live session is one shot with an operator waiting, so everything in
`scripts/verify_nile.py` that can be decided without the network is decided by
code that is tested here: which stages run, whether this configuration is
allowed to move money, what a captured payload says about `tests/fake_tron.py`,
and what ends up written to disk.

The interactive half — the prompts, the wallet, the waiting — is exactly the
part no test can stand in for, which is why the rest has to be certain.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crypto_processing_api.gateway.trongrid import (
    TRONGRID_MAINNET,
    TRONGRID_NILE,
    USDT_CONTRACT_MAINNET,
    USDT_CONTRACT_NILE,
    Trc20Metadata,
)
from scripts.verify_nile import (
    LAST_STAGE,
    Evidence,
    NileFailure,
    Record,
    assert_usdt_contract,
    assert_usdt_payment_method,
    guard_nile_only,
    load_config,
    load_env_file,
    load_record,
    looks_like_tron_address,
    looks_like_txid,
    micros,
    render_log_section,
    save_record,
    shape_diff,
    shape_of,
    stage_needs_nile_config,
    stages_from,
    usdt,
)
from tests.fake_tron import DESTINATION, HOT_WALLET, transaction_info, trc20_metadata_payloads

NILE_ENV = {
    "TRON_NETWORK": "nile",
    "TRONGRID_API_KEY": "a-key",
    "USDT_CONTRACT_ADDRESS": USDT_CONTRACT_NILE,
    "TRON_HOT_WALLET_ADDRESS": HOT_WALLET,
    "TRON_CONFIRMATIONS": "19",
}


# -- configuration ---------------------------------------------------------


def test_the_defaults_are_the_ones_the_service_would_use() -> None:
    config = load_config({})
    assert config.network == "mainnet"
    assert config.nile_endpoint == TRONGRID_NILE
    assert config.nile_contract == USDT_CONTRACT_NILE
    assert config.mainnet_endpoint == TRONGRID_MAINNET
    assert config.mainnet_contract == USDT_CONTRACT_MAINNET
    assert config.confirmations == 19


def test_the_environment_overrides_every_default() -> None:
    config = load_config(
        {**NILE_ENV, "TRONGRID_BASE_URL": "https://nile.example/", "CPAPI_URL": "http://host:9/"}
    )
    assert config.nile_endpoint == "https://nile.example"
    assert config.api_url == "http://host:9"
    assert config.hot_wallet == HOT_WALLET
    assert config.trongrid_key == "a-key"


def test_an_empty_key_reads_as_no_key_not_as_a_key() -> None:
    """A key set to "" in a `.env` is the classic way to be silently keyless."""
    assert load_config({"TRONGRID_API_KEY": "   "}).trongrid_key is None


def test_a_nonsense_confirmation_depth_is_refused() -> None:
    with pytest.raises(NileFailure, match="TRON_CONFIRMATIONS"):
        load_config({"TRON_CONFIRMATIONS": "soon"})


def test_zero_confirmations_is_refused() -> None:
    """Confirming at depth zero would settle a transaction a reorg can remove."""
    with pytest.raises(NileFailure, match="TRON_CONFIRMATIONS"):
        load_config({"TRON_CONFIRMATIONS": "0"})


# -- the mainnet refusal ---------------------------------------------------


def test_a_nile_configuration_is_allowed_to_run_the_drills() -> None:
    guard_nile_only(load_config(NILE_ENV))


def test_the_drills_refuse_to_run_against_mainnet() -> None:
    with pytest.raises(NileFailure, match="TRON_NETWORK is 'mainnet'"):
        guard_nile_only(load_config({**NILE_ENV, "TRON_NETWORK": "mainnet"}))


def test_a_mainnet_endpoint_is_refused_even_when_the_network_says_nile() -> None:
    """`TRON_NETWORK` is a label. The endpoint is where the money is."""
    with pytest.raises(NileFailure, match="not a Nile TronGrid endpoint"):
        guard_nile_only(load_config({**NILE_ENV, "TRONGRID_BASE_URL": TRONGRID_MAINNET}))


def test_the_mainnet_usdt_contract_is_refused_on_a_nile_run() -> None:
    with pytest.raises(NileFailure, match="mainnet USDT contract"):
        guard_nile_only(load_config({**NILE_ENV, "USDT_CONTRACT_ADDRESS": USDT_CONTRACT_MAINNET}))


def test_the_refusal_names_every_reason_at_once() -> None:
    """One reason at a time would mean three failed starts instead of one."""
    with pytest.raises(NileFailure) as caught:
        guard_nile_only(
            load_config(
                {
                    "TRON_NETWORK": "mainnet",
                    "TRONGRID_BASE_URL": TRONGRID_MAINNET,
                    "USDT_CONTRACT_ADDRESS": USDT_CONTRACT_MAINNET,
                }
            )
        )
    message = str(caught.value)
    assert "TRON_NETWORK" in message
    assert "Nile TronGrid endpoint" in message
    assert "mainnet USDT contract" in message


# -- stage sequencing ------------------------------------------------------


def test_a_plain_run_is_every_stage_in_order() -> None:
    assert stages_from(1) == [1, 2, 3, 4, 5, 6]


def test_resuming_skips_only_what_came_before() -> None:
    assert stages_from(3) == [3, 4, 5, 6]
    assert stages_from(LAST_STAGE) == [LAST_STAGE]


@pytest.mark.parametrize("stage", [0, -1, LAST_STAGE + 1])
def test_a_stage_that_does_not_exist_is_refused(stage: int) -> None:
    with pytest.raises(NileFailure, match="there is no stage"):
        stages_from(stage)


def test_only_the_live_stages_need_a_nile_configuration() -> None:
    """5 and 6 read what earlier stages wrote, so they run anywhere."""
    assert [stage for stage in stages_from(1) if stage_needs_nile_config(stage)] == [1, 2, 3, 4]


# -- assertions the preflight makes ----------------------------------------


def test_a_contract_answering_usdt_and_six_passes() -> None:
    assert_usdt_contract(
        network="nile", contract=USDT_CONTRACT_NILE, metadata=Trc20Metadata("USDT", 6)
    )


def test_a_contract_with_another_symbol_is_refused() -> None:
    with pytest.raises(NileFailure, match="not the USDT"):
        assert_usdt_contract(
            network="nile", contract=USDT_CONTRACT_NILE, metadata=Trc20Metadata("WTRX", 6)
        )


def test_the_wrong_decimals_is_refused_even_with_the_right_symbol() -> None:
    """18 decimals against a 6-decimal asset is every amount wrong by 10^12."""
    with pytest.raises(NileFailure, match="decimals\\(\\)=18"):
        assert_usdt_contract(
            network="nile", contract=USDT_CONTRACT_NILE, metadata=Trc20Metadata("USDT", 18)
        )


@pytest.mark.parametrize("method", ["USDT-TRON", "USDT_TRX", "usdt-tron"])
def test_the_plugins_payment_method_is_recognised_in_the_shapes_it_ships(method: str) -> None:
    assert_usdt_payment_method(["BTC-CHAIN", method])


def test_a_store_with_no_usdt_method_is_refused_with_what_it_does_have() -> None:
    with pytest.raises(NileFailure, match="BTC-CHAIN"):
        assert_usdt_payment_method(["BTC-CHAIN"])


def test_an_empty_store_is_refused_readably() -> None:
    with pytest.raises(NileFailure, match=r"\[\]"):
        assert_usdt_payment_method([])


# -- what the operator is allowed to paste ---------------------------------


@pytest.mark.parametrize("value", ["a" * 64, "0x" + "F" * 64])
def test_a_transaction_id_is_sixty_four_hex_characters(value: str) -> None:
    assert looks_like_txid(value)


@pytest.mark.parametrize("value", ["", "a" * 63, "a" * 65, "z" * 64, HOT_WALLET])
def test_anything_else_is_not_a_transaction_id(value: str) -> None:
    assert not looks_like_txid(value)


def test_a_tron_address_is_recognised() -> None:
    assert looks_like_tron_address(HOT_WALLET)


@pytest.mark.parametrize("value", ["", "0x0000000000000000000000000000000000000000", "T"])
def test_an_ethereum_address_is_not_a_tron_one(value: str) -> None:
    assert not looks_like_tron_address(value)


# -- amounts ---------------------------------------------------------------


def test_a_decimal_amount_becomes_exact_micro_usdt() -> None:
    assert micros("5.000000") == 5_000_000
    assert micros("0.000001") == 1
    assert micros("199.000000") == 199_000_000


def test_micro_usdt_renders_back_to_six_decimals() -> None:
    assert usdt(1) == "0.000001"
    assert usdt(5_000_000) == "5.000000"


# -- payload shapes --------------------------------------------------------


def test_a_payload_reduces_to_a_field_map() -> None:
    shape = shape_of({"id": "a", "receipt": {"result": "SUCCESS"}, "log": [{"data": "00"}]})
    assert shape["id"] == "str"
    assert shape["receipt.result"] == "str"
    assert shape["log"] == "list"
    assert shape["log[0].data"] == "str"


def test_an_identical_payload_has_no_differences() -> None:
    payload = transaction_info(txid="a" * 64, recipient=DESTINATION, amount=1_000_000)
    assert shape_diff(live=payload, fake=payload) == []


def test_a_field_only_the_live_payload_has_is_reported_as_new() -> None:
    payload = transaction_info(txid="a" * 64, recipient=DESTINATION, amount=1_000_000)
    live = {**payload, "unfreeze_amount": 0}
    lines = shape_diff(live=live, fake=payload)
    assert lines == ["+ `unfreeze_amount`: live payload has int, the fake has no such field"]


def test_a_field_only_the_fake_has_is_reported_as_missing() -> None:
    payload = transaction_info(txid="a" * 64, recipient=DESTINATION, amount=1_000_000)
    live = {key: value for key, value in payload.items() if key != "blockTimeStamp"}
    lines = shape_diff(live=live, fake=payload)
    assert lines == ["- `blockTimeStamp`: the fake has int, the live payload has no such field"]


def test_a_field_that_changed_type_is_reported_as_a_change() -> None:
    """`blockNumber` arriving as a string is the difference that would matter."""
    payload = transaction_info(txid="a" * 64, recipient=DESTINATION, amount=1_000_000)
    live = {**payload, "blockNumber": "1000"}
    assert shape_diff(live=live, fake=payload) == [
        "~ `blockNumber`: live payload has str, the fake has int"
    ]


def test_a_difference_inside_a_log_entry_is_found() -> None:
    payload = transaction_info(txid="a" * 64, recipient=DESTINATION, amount=1_000_000)
    live = json.loads(json.dumps(payload))
    live["log"][0]["blockNumber"] = 1_000
    assert shape_diff(live=live, fake=payload) == [
        "+ `log[0].blockNumber`: live payload has int, the fake has no such field"
    ]


def test_the_metadata_payload_the_fake_builds_matches_itself() -> None:
    symbol, decimals = trc20_metadata_payloads()
    assert shape_diff(live=symbol, fake=symbol) == []
    assert shape_diff(live=symbol, fake=decimals) == []


# -- evidence on disk ------------------------------------------------------


def test_a_payload_is_written_where_the_next_run_can_find_it(tmp_path: Path) -> None:
    evidence = Evidence(tmp_path)
    written = evidence.record(
        source="nile/wallet/gettransactioninfobyid", request={"value": "a"}, response={"id": "a"}
    )
    assert written.name == "001-nile-wallet-gettransactioninfobyid.json"
    stored = json.loads(written.read_text(encoding="utf-8"))
    assert stored["request"] == {"value": "a"}
    assert stored["response"] == {"id": "a"}
    assert stored["recorded_at"]


def test_payloads_are_numbered_in_the_order_they_arrived(tmp_path: Path) -> None:
    evidence = Evidence(tmp_path)
    evidence.record(source="one", request={}, response={})
    second = evidence.record(source="two", request={}, response={})
    assert second.name.startswith("002-")


def test_a_later_run_picks_up_what_an_earlier_one_captured(tmp_path: Path) -> None:
    """`--stage 5` in a new terminal has to diff payloads it did not fetch."""
    first = Evidence(tmp_path)
    first.record(source="nile/wallet/gettransactioninfobyid", request={}, response={"id": "AB12"})

    resumed = Evidence(tmp_path)
    assert len(resumed.entries) == 1
    assert resumed.transaction("ab12") == {"id": "AB12"}
    assert resumed.record(source="next", request={}, response={}).name.startswith("002-")


def test_an_unknown_transaction_is_absent_rather_than_wrong(tmp_path: Path) -> None:
    evidence = Evidence(tmp_path)
    evidence.record(source="nile/wallet/gettransactioninfobyid", request={}, response={"id": "aa"})
    assert evidence.transaction("bb") is None


def test_an_unreadable_evidence_file_does_not_stop_the_session(tmp_path: Path) -> None:
    (tmp_path / "001-broken.json").write_text("{not json", encoding="utf-8")
    assert Evidence(tmp_path).entries == []


def test_responses_are_selectable_by_where_they_came_from(tmp_path: Path) -> None:
    evidence = Evidence(tmp_path)
    evidence.record(source="nile/wallet/triggerconstantcontract", request={}, response={"a": 1})
    evidence.record(source="mainnet/wallet/triggerconstantcontract", request={}, response={"b": 2})
    evidence.record(source="nile/wallet/getnowblock", request={}, response={"c": 3})
    assert evidence.responses("triggerconstantcontract") == [{"a": 1}, {"b": 2}]


# -- session state ---------------------------------------------------------


def test_a_missing_state_file_is_a_fresh_session(tmp_path: Path) -> None:
    assert load_record(tmp_path / "session.json") == Record()


def test_state_survives_a_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "session.json"
    record = Record(deposit_id="dep-1", withdrawal_txid="ab" * 32)
    record.mark_done(2)
    save_record(record, path)
    assert load_record(path) == record


def test_a_state_file_from_an_older_script_still_loads(tmp_path: Path) -> None:
    """A field this version does not know must not end a session mid-drill."""
    path = tmp_path / "session.json"
    path.write_text(json.dumps({"deposit_id": "dep-1", "gone_field": 7}), encoding="utf-8")
    assert load_record(path).deposit_id == "dep-1"


def test_a_stage_is_only_recorded_once(tmp_path: Path) -> None:
    record = Record()
    record.mark_done(3)
    record.mark_done(1)
    record.mark_done(3)
    assert record.stages_done == [1, 3]


# -- the env file ----------------------------------------------------------


def test_the_env_file_is_read_the_way_compose_reads_it(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "# a comment\n\nTRON_NETWORK=nile\nUSDT_CONTRACT_ADDRESS='TAbc'\nbroken-line\n",
        encoding="utf-8",
    )
    assert load_env_file(path) == {"TRON_NETWORK": "nile", "USDT_CONTRACT_ADDRESS": "TAbc"}


def test_no_env_file_is_not_an_error(tmp_path: Path) -> None:
    assert load_env_file(tmp_path / "nope") == {}


# -- the report ------------------------------------------------------------


def filled_record() -> Record:
    record = Record(
        started_at="2026-08-20T10:00:00+00:00",
        finished_at="2026-08-20T10:41:00+00:00",
        network="nile",
        nile_endpoint=TRONGRID_NILE,
        nile_contract=USDT_CONTRACT_NILE,
        nile_symbol="USDT",
        nile_decimals=6,
        mainnet_contract=USDT_CONTRACT_MAINNET,
        mainnet_symbol="USDT",
        mainnet_decimals=6,
        confirmations_required=19,
        deposit_address=DESTINATION,
        deposit_credited_micro=5_000_000,
        deposit_txid="de" * 32,
        withdrawal_destination=DESTINATION,
        withdrawal_gross_micro=2_000_000,
        withdrawal_fee_micro=1_000_000,
        withdrawal_net_micro=1_000_000,
        withdrawal_txid="wd" * 32,
        withdrawal_tx_block=1_000,
        withdrawal_confirm_height=1_021,
        withdrawal_confirmations_seen=21,
        duplicate_withdrawal_id="w-2",
        duplicate_status=409,
        duplicate_detail="transaction wdwd is already recorded against withdrawal w-1",
    )
    return record


def test_the_report_carries_every_number_the_log_asks_for() -> None:
    section = render_log_section(filled_record())
    assert "## Run of 2026-08-20" in section
    assert USDT_CONTRACT_NILE in section
    assert USDT_CONTRACT_MAINNET in section
    assert "de" * 32 in section
    assert "wd" * 32 in section
    assert "5.000000" in section
    assert "1.000000" in section
    assert "409" in section
    assert "21 blocks deep" in section
    assert "`TRON_CONFIRMATIONS` is 19" in section


def test_the_report_says_so_when_the_fake_already_matched() -> None:
    assert "none — the fake matched" in render_log_section(filled_record())


def test_every_shape_difference_reaches_the_report() -> None:
    record = filled_record()
    record.shape_diffs = ["gettransactioninfobyid + `fee`: live payload has int"]
    assert "gettransactioninfobyid + `fee`" in render_log_section(record)


def test_an_empty_record_still_renders_rather_than_crashing() -> None:
    """The report stage runs after a failed session too, and that is when the
    partial evidence is worth the most."""
    section = render_log_section(Record())
    assert "## Run of —" in section

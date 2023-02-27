from hexbytes import HexBytes

from tests.conftest import get_blockstamp_by_state


# ----- Hash calculations ----------
def test_hash_calculations(consensus):
    report_data = (1, 2, 3, 4, [5, 6], [7, 8], 9, 10, 11, 12, True, 13, HexBytes(int.to_bytes(14, 32)), 15)
    report_hash = consensus._get_report_hash(report_data)
    assert isinstance(report_hash, HexBytes)
    assert report_hash == b'\x10\xb7_&\xde\r\\\xbc\xc6a\xb5\xa1\x83u\xf6\x14\xf6:\xf9\r6:\x8cQ\xf6\xb2^\xffG\xee\xf5\xc1'


# ------ Process report hash -----------
def test_report_hash(web3, consensus, tx_utils, set_report_account):
    latest_blockstamp = get_blockstamp_by_state(web3, 'head')
    consensus._process_report_hash(latest_blockstamp, HexBytes(int.to_bytes(1, 32)))
    # TODO add check that report hash was submitted


def test_do_not_report_same_hash(web3, consensus, caplog):
    latest_blockstamp = get_blockstamp_by_state(web3, 'head')
    member_info = consensus._get_member_info(latest_blockstamp)

    consensus._process_report_hash(latest_blockstamp, HexBytes(member_info.current_frame_member_report))
    assert "Provided hash already submitted" in caplog.messages[-1]


# -------- Process report data ---------
def test_process_report_data_hash_not_actualized(web3, consensus, caplog):
    blockstamp = get_blockstamp_by_state(web3, "head")
    report_data = tuple()
    report_hash = int.to_bytes(1, 32)
    consensus._process_report_data(blockstamp, report_data, report_hash)
    assert "Quorum is not ready and member did not send the report hash." in caplog.messages[-1]


def test_process_report_data_wait_for_consensus(consensus):
    pass


def test_process_report_data_hash_differs_from_quorums(consensus):
    pass


def test_process_report_data_main_data_submitted(consensus):
    # Check there is no sleep
    pass


def test_process_report_data_main_sleep_until_data_submitted(consensus):
    # It should wake in half of the sleep
    pass


def test_process_report_data_sleep_ends(consensus):
    # No infinity sleep?
    pass


# ----- Test sleep calculations
def test_get_slot_delay_before_data_submit(consensus):
    pass
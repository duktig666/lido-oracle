import logging
from collections import defaultdict
from typing import List, Generator, Union

from hexbytes import HexBytes
from web3 import Web3
from web3.types import TxParams

from src.contracts import contracts
from src.modules.interface import OracleModule
from src.providers.beacon import BeaconChainClient
from src.web3_utils.tx_execution import check_transaction, sign_and_send_transaction

from src.web3_utils.contract_frame_utils import is_current_epoch_reportable
from src.web3_utils.typings import ValidatorGroup, SlotNumber, MergedLidoValidator, ValidatorStatus
from src.web3_utils.validators import get_lido_validators
from src.variables import ACCOUNT, GAS_LIMIT

logger = logging.getLogger(__name__)


class Ejector(OracleModule):
    """
    Ejector (Optional Oracle Module)

    Decides how many validators should exit if we receive withdrawal requests.

    Logic:
    1. Checks how many ETH want to be withdrawn. (width_eth)
    2. Calculates ETH buffered on lido contract. (buff_eth)
    3. Calculates ETH on rewards contract. (reward_eth)
    4. Predicts rewards for one day. (predict_reward_eth)
    5. Get balance going to withdraw validators. (width_eth)
    7. Validators to withdraw - (width_eth - buff_eth - reward_eth - predict_reward_eth - width_eth) / 32 ETH

    All calculations done in wei.
    """

    def __init__(self, web3: Web3, beacon_client: BeaconChainClient):
        logger.info({'msg': 'Initialize Ejector module'})
        self._w3 = web3
        self._beacon_chain_client = beacon_client

        self._update_beacon_specs()

    def _update_beacon_specs(self, block_identifier: Union[str, HexBytes] = 'latest'):
        (
            self.epochs_per_frame,
            self.slots_per_epoch,
            self.seconds_per_slot,
            self.genesis_time,
        ) = contracts.validator_exit_bus.functions.getBeaconSpec().call(block_identifier=block_identifier)
        logging.info({
            'msg': 'Update beacon specs.',
            'epochs_per_frame': self.epochs_per_frame,
            'slots_per_epoch': self.slots_per_epoch,
            'seconds_per_slot': self.seconds_per_slot,
            'genesis_time': self.genesis_time,
        })

    def run_module(self, slot: SlotNumber, block_hash: HexBytes):
        """Get validators count to eject and create ejection event."""
        logger.info({'msg': 'Execute ejector.', 'block_hash': block_hash})
        self._update_beacon_specs()

        if not is_current_epoch_reportable(self._w3, contracts.validator_exit_bus, slot, block_hash):
            logger.info({'msg': 'Epoch is not reportable.'})
            return

        wei_amount_to_eject = self.calc_wei_amount_to_eject(slot, block_hash)

        # TODO remove
        wei_amount_to_eject = 10**19

        if wei_amount_to_eject > 0:
            logger.info({'msg': 'Start ejecting validators.'})
            self.eject_validators(wei_amount_to_eject, slot, block_hash)

    def calc_wei_amount_to_eject(self, slot: SlotNumber, block_hash: HexBytes) -> int:
        """Calculate validators count to eject."""
        amount_wei_to_withdraw = self._get_withdrawal_requests_wei_amount(block_hash)
        logger.info({'msg': 'Calculate wei in withdrawal queue.', 'value': amount_wei_to_withdraw})

        buffered_eth = self._get_buffered_eth(block_hash)
        logger.info({'msg': 'Calculate wei in buffer.', 'value': buffered_eth})

        el_rewards_eth = self._get_el_rewards(block_hash)
        logger.info({'msg': 'Calculate rewards.', 'value': el_rewards_eth})

        el_predicted_rewards = self._get_predicted_el_rewards(block_hash)
        logger.info({'msg': 'Calculate predicted rewards.', 'value': el_predicted_rewards})

        skimmed_rewards = self._get_skimmed_rewards(block_hash)
        logger.info({'msg': 'Get skimmed rewards.', 'value': skimmed_rewards})

        skimmed_predicted_rewards = self._get_predicted_skimmed_rewards(block_hash)
        logger.info({'msg': 'Get skimmed rewards fo next day.', 'value': skimmed_predicted_rewards})

        exiting_validators_balances = self._get_exiting_validators_balances(slot, block_hash)
        logger.info({'msg': 'Get exiting validators balances.', 'value': exiting_validators_balances})

        current_ether = buffered_eth + el_rewards_eth + skimmed_rewards + exiting_validators_balances
        logger.info({'msg': 'Calculate ether that will be available to withdraw.', 'value': current_ether})

        current_and_predicted_eth = current_ether + el_predicted_rewards + skimmed_predicted_rewards
        logger.info({
            'msg': 'Calculate ether that will be available to withdraw in one day.',
            'value': current_and_predicted_eth,
        })

        result = amount_wei_to_withdraw - current_and_predicted_eth
        logger.info({'msg': 'Wei to eject.', 'value': result})

        return result

    def _get_withdrawal_requests_wei_amount(self, block_hash: HexBytes) -> int:
        total_pooled_ether = contracts.lido.functions.getTotalPooledEther().call(block_identifier=block_hash)
        logger.info({'msg': 'Get total pooled ether.', 'value': total_pooled_ether})

        total_shares = contracts.lido.functions.getSharesByPooledEth(total_pooled_ether).call(block_identifier=block_hash)
        logger.info({'msg': 'Get total shares.', 'value': total_shares})

        queue_length = contracts.withdrawal_queue.functions.queueLength().call(block_identifier=block_hash)
        logger.info({'msg': 'Get last id to finalize.', 'value': total_pooled_ether})

        if queue_length == 0:
            logger.info({'msg': 'Withdrawal queue is empty.'})
            return 0

        return contracts.withdrawal_queue.functions.calculateFinalizationParams(
            queue_length - 1,
            total_pooled_ether,
            total_shares,
        ).call(block_identifier=block_hash)

    def _get_buffered_eth(self, block_hash: HexBytes) -> int:
        # reserved_buffered_eth = contracts.lido.functions.getReservedBufferedEther().call(block_identifier=block_hash)
        reserved_buffered_eth = 0
        logger.info({'msg': 'Get reserved buffered eth.', 'value': reserved_buffered_eth})

        current_buffered_eth = contracts.lido.functions.getBufferedEther().call(block_identifier=block_hash)
        logger.info({'msg': 'Get current buffered eth.', 'value': current_buffered_eth})

        return min(reserved_buffered_eth, current_buffered_eth)

    def _get_el_rewards(self, block_hash: HexBytes) -> int:
        return self._w3.eth.get_balance(
            contracts.lido.functions.getELRewardsVault().call(block_identifier=block_hash),
            block_identifier=block_hash,
        )

    def _get_predicted_el_rewards(self, block_hash: HexBytes) -> int:
        return self._get_all_income_for_frame_to_address(
            block_hash,
            contracts.lido.functions.getELRewardsVault().call(block_identifier=block_hash),
        )

    def _get_skimmed_rewards(self, block_hash: HexBytes) -> int:
        return self._w3.eth.get_balance(
            self._get_wc_address(block_hash),
            block_identifier=block_hash,
        )

    def _get_predicted_skimmed_rewards(self, block_hash: HexBytes) -> int:
        return self._get_all_income_for_frame_to_address(
            block_hash,
            self._get_wc_address(block_hash),
        )

    def _get_wc_address(self, block_hash: HexBytes):
        wc = contracts.lido.functions.getWithdrawalCredentials().call(block_identifier=block_hash)

        address = self._w3.toChecksumAddress('0x' + wc.hex()[-40:])

        return address

    def _get_all_income_for_frame_to_address(self, block_hash: HexBytes, address: str) -> int:
        blocks_in_frame = self.epochs_per_frame * self.slots_per_epoch
        return 0

    def _get_exiting_validators_balances(self, slot: SlotNumber, block_hash: HexBytes) -> int:
        validators = get_lido_validators(self._w3, block_hash, self._beacon_chain_client, slot)

        exiting_balance = 0

        for validator in validators:
            if validator['validator']['status'] in ValidatorGroup.GOING_TO_EXIT:
                exiting_balance += validator['validator']['balance']

        return exiting_balance * 10**9

    def eject_validators(self, wei_amount: int, slot: SlotNumber, block_hash: HexBytes):
        validators_to_eject = self._get_keys_to_eject(wei_amount, slot, block_hash)
        logger.info({'msg': f'Get list validators to eject. Validators count: {len(validators_to_eject)}'})

        if validators_to_eject:
            self._submit_keys_ejection(validators_to_eject, slot)

    def _get_keys_to_eject(
        self,
        wei_amount_to_eject: int,
        slot: SlotNumber,
        block_hash: HexBytes,
    ) -> List[MergedLidoValidator]:
        """Get NO operator's keys with bigger amount of keys"""
        validators = get_lido_validators(self._w3, block_hash, self._beacon_chain_client, slot)
        current_validators_wei_amount = 0
        validators_to_eject = []

        eject_validator_generator = self._get_validator_to_eject(validators)

        while current_validators_wei_amount < wei_amount_to_eject:
            try:
                validator = next(eject_validator_generator)
            except StopIteration:
                logger.info({'msg': 'Exited maximum validators.'})
                return validators_to_eject

            current_validators_wei_amount += min(int(validator['validator']['balance']) * 10**9, 32 * 10**18)

            validators_to_eject.append(validator)

        return validators_to_eject

    def _get_validator_to_eject(
        self,
        lido_validators: List[MergedLidoValidator],
    ) -> Generator[MergedLidoValidator, None, None]:
        """
        Filter all exiting and exited validators.
        Sort them by index.
        Return by one validator from the node operator that have the largest amount of working validators.
        """
        validators = filter(lambda val: val['validator']['status'] == ValidatorStatus.ACTIVE_ONGOING.value, lido_validators)
        validators = sorted(validators, key=lambda val: val['validator']['index'])

        operators_validators = defaultdict(list)

        for validator in validators:
            operators_validators[(validator['key']['module_id'], validator['key']['operator_index'])].append(validator)

        for module_id, node_operator in operators_validators.keys():
            # Filter all validators
            last_validator_index = contracts.validator_exit_bus.functions.getLastRequestedValidatorId(module_id, node_operator).call()

            operators_validators[module_id, node_operator] = list(filter(
                lambda val: int(val['validator']['index']) > last_validator_index,
                operators_validators[module_id, node_operator],
            ))


        while True:
            max_using_validators_by_no = 0
            operator_key = (0, 0)

            for key, validators in operators_validators.items():
                current_no_validator_count = len(validators)

                if current_no_validator_count > max_using_validators_by_no:
                    operator_key = key
                    max_using_validators_by_no = current_no_validator_count
                elif current_no_validator_count == max_using_validators_by_no:
                    if key[0] < operator_key[0]:
                        operator_key = key
                    elif key[1] < operator_key[1]:
                        operator_key = key

            yield operators_validators[operator_key].pop(0)

    def _submit_keys_ejection(self, validators_list: List[MergedLidoValidator], slot: SlotNumber):
        logger.info({'msg': 'Start ejecting.', 'value': len(validators_list)})

        ejection_report = self._prepare_transaction(validators_list, slot)

        if check_transaction(ejection_report, ACCOUNT.address):
            sign_and_send_transaction(self._w3, ejection_report, ACCOUNT, GAS_LIMIT)

    def _prepare_transaction(
            self,
            validators_list: List[MergedLidoValidator],
            slot: SlotNumber,
    ) -> TxParams:
        modules_id = []
        node_operators_id = []
        validator_id =[]
        validators_pub_keys = []

        for validator in validators_list:
            modules_id.append(validator['key']['module_id'])
            node_operators_id.append(validator['key']['operator_index'])
            validator_id.append(int(validator['validator']['index']))
            validators_pub_keys.append(validator['key']['key'])

        ejection_report = contracts.validator_exit_bus.functions.handleCommitteeMemberReport(
            modules_id,
            node_operators_id,
            validator_id,
            validators_pub_keys,
            int(slot/self.slots_per_epoch),
        )

        logger.info({
            'msg': 'Build ejection report.',
            'value': [modules_id, node_operators_id, validators_pub_keys],
        })

        return ejection_report
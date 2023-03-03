import sys

from prometheus_client import start_http_server
from web3_multi_provider import MultiProvider  # type: ignore[import]
from web3.middleware import simple_cache_middleware

from src import variables
from src.metrics.healthcheck_server import start_pulse_server
from src.metrics.logging import logging
from src.modules.accounting.accounting import Accounting
from src.modules.ejector.ejector import Ejector
from src.typings import OracleModule
from src.web3py.extensions import (
    LidoContracts,
    TransactionUtils,
    ConsensusClientModule,
    KeysAPIClientModule,
    LidoValidatorsProvider,
)
from src.web3py.middleware import metrics_collector
from src.web3py.typings import Web3

from src.web3py.contract_tweak import tweak_w3_contracts


logger = logging.getLogger()


def main(module_name: OracleModule):
    logger.info({
        'msg': 'Oracle startup.',
        'variables': {
            'module': module_name,
            'ACCOUNT': variables.ACCOUNT.address if variables.ACCOUNT else 'Dry',
            'LIDO_LOCATOR_ADDRESS': variables.LIDO_LOCATOR_ADDRESS,
            'MAX_CYCLE_LIFETIME_IN_SECONDS': variables.MAX_CYCLE_LIFETIME_IN_SECONDS,
        },
    })

    logger.info({'msg': f'Start healthcheck server for Docker container on port {variables.HEALTHCHECK_SERVER_PORT}'})
    start_pulse_server()

    logger.info({'msg': f'Start http server with prometheus metrics on port {variables.PROMETHEUS_PORT}'})
    start_http_server(variables.PROMETHEUS_PORT)

    logger.info({'msg': 'Initialize multi web3 provider.'})
    web3 = Web3(MultiProvider(variables.EXECUTION_CLIENT_URI))

    logger.info({'msg': 'Modify web3 with custom contract function call.'})
    tweak_w3_contracts(web3)

    web3.attach_modules({
        'lido_contracts': LidoContracts,
        'lido_validators': LidoValidatorsProvider,
        'transaction': TransactionUtils,
        'cc': lambda: ConsensusClientModule(variables.CONSENSUS_CLIENT_URI, web3),  # type: ignore[dict-item]
        'kac': lambda: KeysAPIClientModule(variables.KEYS_API_URI, web3),  # type: ignore[dict-item]
    })

    logger.info({'msg': 'Add metrics middleware for ETH1 requests.'})
    web3.middleware_onion.add(metrics_collector)
    web3.middleware_onion.add(simple_cache_middleware)

    if module_name == OracleModule.ACCOUNTING:
        logger.info({'msg': 'Initialize Accounting module.'})
        accounting = Accounting(web3)
        accounting.run_as_daemon()
    elif module_name == OracleModule.EJECTOR:
        logger.info({'msg': 'Initialize Ejector module.'})
        ejector = Ejector(web3)
        ejector.run_as_daemon()


if __name__ == '__main__':
    last_arg = sys.argv[-1]
    if last_arg not in iter(OracleModule):
        msg = f'Last arg should be one of {[str(item) for item in OracleModule]}, received {last_arg}.'
        logger.error({'msg': msg})
        raise ValueError(msg)

    main(OracleModule(last_arg))
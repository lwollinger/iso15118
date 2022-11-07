from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from iso15118.secc import Config
from iso15118.secc.states.iso15118_2_states import (
    Authorization,
    ChargeParameterDiscovery,
    ChargingStatus,
    CurrentDemand,
    PaymentDetails,
    PowerDelivery,
    ServiceDiscovery,
    SessionStop,
    Terminate,
    WeldingDetection,
)
from iso15118.secc.states.secc_state import StateSECC
from iso15118.shared.messages.datatypes import EVSENotification
from iso15118.shared.messages.enums import (
    AuthEnum,
    AuthorizationStatus,
    AuthorizationTokenType,
    CpState,
    EVSEProcessing,
)
from iso15118.shared.messages.iso15118_2.body import ResponseCode
from iso15118.shared.messages.iso15118_2.datatypes import (
    ACEVSEStatus,
    CertificateChain,
    ChargingSession,
)
from iso15118.shared.security import get_random_bytes
from iso15118.shared.states import Pause
from tests.secc.states.test_messages import (
    get_charge_parameter_discovery_req_message_departure_time_one_hour,
    get_charge_parameter_discovery_req_message_no_departure_time,
    get_dummy_charging_status_req,
    get_dummy_v2g_message_authorization_req,
    get_dummy_v2g_message_payment_details_req,
    get_dummy_v2g_message_power_delivery_req_charge_start,
    get_dummy_v2g_message_power_delivery_req_charge_stop,
    get_dummy_v2g_message_service_discovery_req,
    get_dummy_v2g_message_welding_detection_req,
    get_dummy_v2g_session_stop_req,
    get_v2g_message_power_delivery_req,
)


@patch("iso15118.shared.states.EXI.to_exi", new=Mock(return_value=b"01"))
@pytest.mark.asyncio
class TestV2GSessionScenarios:
    @pytest.fixture(autouse=True)
    def _comm_session(self, comm_secc_session_mock):
        self.comm_session = comm_secc_session_mock
        self.comm_session.config = Config()
        self.comm_session.is_tls = False
        self.comm_session.writer = Mock()
        self.comm_session.writer.get_extra_info = Mock()

    async def test_current_demand_to_power_delivery_when_power_delivery_received(
        self,
    ):
        current_demand = CurrentDemand(self.comm_session)
        current_demand.expecting_current_demand_req = False
        await current_demand.process_message(
            message=get_v2g_message_power_delivery_req()
        )
        assert isinstance(self.comm_session.current_state, PowerDelivery)

    async def test_power_delivery_to_welding_detection_when_welding_detection_received(
        self,
    ):
        # V2G2-601 (to WeldingDetection)
        power_delivery = PowerDelivery(self.comm_session)
        power_delivery.expecting_power_delivery_req = False
        await power_delivery.process_message(
            message=get_dummy_v2g_message_welding_detection_req()
        )
        assert isinstance(self.comm_session.current_state, WeldingDetection)

    async def test_welding_detection_to_session_stop_when_session_stop_received(
        self,
    ):
        pass
        # V2G2-570

    @patch.object(
        PaymentDetails,
        "_mobility_operator_root_cert_path",
        return_value=Path(__file__).parent.parent.parent
        / "sample_certs"
        / "moRootCACert.der",
    )
    @pytest.mark.parametrize(
        "is_authorized_return_value, expected_next_state",
        [
            (AuthorizationStatus.ACCEPTED, Authorization),
            (AuthorizationStatus.ONGOING, Authorization),
            (AuthorizationStatus.REJECTED, Terminate),
        ],
    )
    async def test_payment_details_next_state_on_payment_details_req_auth(
        self,
        mo_root_cert_path_mock,
        is_authorized_return_value: AuthorizationStatus,
        expected_next_state: StateSECC,
    ):

        self.comm_session.selected_auth_option = AuthEnum.PNC_V2
        mock_is_authorized = AsyncMock(return_value=is_authorized_return_value)
        self.comm_session.evse_controller.is_authorized = mock_is_authorized
        payment_details = PaymentDetails(self.comm_session)
        payment_details_req = get_dummy_v2g_message_payment_details_req()
        await payment_details.process_message(payment_details_req)

        assert isinstance(
            self.comm_session.contract_cert_chain, CertificateChain
        ), "Comm session certificate chain not populated"
        assert (
            payment_details.next_state == expected_next_state
        ), "State did not progress after PaymentDetailsReq"
        mock_is_authorized.assert_called_once()
        req_body = payment_details_req.body.payment_details_req
        assert mock_is_authorized.call_args[1]["id_token"] == req_body.emaid
        assert mock_is_authorized.call_args[1]["id_token_type"] == (
            AuthorizationTokenType.EMAID
        )

    @pytest.mark.parametrize(
        "auth_type, is_authorized_return_value, expected_next_state,"
        "expected_response_code, expected_evse_processing",
        [
            (
                AuthEnum.EIM,
                AuthorizationStatus.ACCEPTED,
                ChargeParameterDiscovery,
                ResponseCode.OK,
                EVSEProcessing.FINISHED,
            ),
            (
                AuthEnum.EIM,
                AuthorizationStatus.ONGOING,
                None,
                ResponseCode.OK,
                EVSEProcessing.ONGOING,
            ),
            (
                AuthEnum.EIM,
                AuthorizationStatus.REJECTED,
                Terminate,
                ResponseCode.FAILED,
                EVSEProcessing.FINISHED,
            ),
            (
                AuthEnum.PNC_V2,
                AuthorizationStatus.ACCEPTED,
                ChargeParameterDiscovery,
                ResponseCode.OK,
                EVSEProcessing.FINISHED,
            ),
            (
                AuthEnum.PNC_V2,
                AuthorizationStatus.ONGOING,
                None,
                ResponseCode.OK,
                EVSEProcessing.ONGOING,
            ),
            (
                AuthEnum.PNC_V2,
                AuthorizationStatus.REJECTED,
                Terminate,
                ResponseCode.FAILED,
                EVSEProcessing.FINISHED,
            ),
        ],
    )
    async def test_authorization_next_state_on_authorization_request(
        self,
        auth_type: AuthEnum,
        is_authorized_return_value: AuthorizationStatus,
        expected_next_state: StateSECC,
        expected_response_code: ResponseCode,
        expected_evse_processing: EVSEProcessing,
    ):

        self.comm_session.selected_auth_option = auth_type
        mock_is_authorized = AsyncMock(return_value=is_authorized_return_value)
        self.comm_session.evse_controller.is_authorized = mock_is_authorized
        # TODO: Include a real CertificateChain object and a message header
        #       with a signature that must be return by
        #      `get_dummy_v2g_message_authorization_req`
        self.comm_session.contract_cert_chain = Mock()
        self.comm_session.emaid = "dummy"
        self.comm_session.gen_challenge = None
        authorization = Authorization(self.comm_session)
        authorization.signature_verified_once = True
        await authorization.process_message(
            message=get_dummy_v2g_message_authorization_req()
        )
        assert authorization.next_state == expected_next_state
        assert (
            authorization.message.body.authorization_res.response_code
            == expected_response_code
        )
        assert (
            authorization.message.body.authorization_res.evse_processing
            == expected_evse_processing
        )

    async def test_authorization_req_gen_challenge_invalid(self):
        self.comm_session.selected_auth_option = AuthEnum.PNC_V2
        self.comm_session.contract_cert_chain = Mock()
        self.comm_session.gen_challenge = get_random_bytes(16)
        id = "aReq"
        gen_challenge = get_random_bytes(16)
        authorization = Authorization(self.comm_session)

        await authorization.process_message(
            message=get_dummy_v2g_message_authorization_req(id, gen_challenge)
        )
        assert authorization.next_state == Terminate
        assert (
            authorization.message.body.authorization_res.response_code
            == ResponseCode.FAILED_CHALLENGE_INVALID
        )

    async def test_authorization_req_gen_challenge_valid(self):
        self.comm_session.selected_auth_option = AuthEnum.PNC_V2
        self.comm_session.gen_challenge = get_random_bytes(16)
        id = "aReq"
        gen_challenge = self.comm_session.gen_challenge
        self.comm_session.contract_cert_chain = Mock()
        self.comm_session.emaid = "dummy"
        authorization = Authorization(self.comm_session)
        authorization.signature_verified_once = True
        await authorization.process_message(
            message=get_dummy_v2g_message_authorization_req(id, gen_challenge)
        )
        assert authorization.next_state == ChargeParameterDiscovery

    async def test_charge_parameter_discovery_res_v2g2_303(self):
        # V2G2-303 : Sum of individual time intervals shall match the period of time
        # indicated by the EVCC.
        charge_parameter_discovery = ChargeParameterDiscovery(self.comm_session)

        charge_parameter_discovery_req_departure_time_set = (
            get_charge_parameter_discovery_req_message_departure_time_one_hour()
        )
        await charge_parameter_discovery.process_message(
            message=charge_parameter_discovery_req_departure_time_set
        )

        charging_duration = (
            charge_parameter_discovery_req_departure_time_set.body.charge_parameter_discovery_req.ac_ev_charge_parameter.departure_time  # noqa
        )

        assert (
            charge_parameter_discovery.message.body.charge_parameter_discovery_res
            is not None
        )
        charge_parameter_discovery_res = (
            charge_parameter_discovery.message.body.charge_parameter_discovery_res
        )
        assert charge_parameter_discovery_res.sa_schedule_list is not None
        sa_schedule_tuples = (
            charge_parameter_discovery_res.sa_schedule_list.schedule_tuples
        )
        for schedule_tuples in sa_schedule_tuples:
            assert schedule_tuples.p_max_schedule is not None
            schedule_duration = 0
            if schedule_tuples.p_max_schedule.schedule_entries is not None:
                first_entry_start_time = (
                    schedule_tuples.p_max_schedule.schedule_entries[
                        0
                    ].time_interval.start
                )
                last_entry_start_time = schedule_tuples.p_max_schedule.schedule_entries[
                    -1
                ].time_interval.start
                last_entry_schedule_duration = (
                    schedule_tuples.p_max_schedule.schedule_entries[
                        -1
                    ].time_interval.duration
                )
                schedule_duration = (
                    last_entry_start_time - first_entry_start_time
                ) + last_entry_schedule_duration

            assert schedule_duration == charging_duration

    async def test_charge_parameter_discovery_res_v2g2_304(self):
        # V2G2-304: If departure time was not provided, then sum of time intervals
        # in PMaxSchedule shall be greater than or equal to 24 hours.
        twenty_four_hours_in_seconds = 86400
        charge_parameter_discovery = ChargeParameterDiscovery(self.comm_session)
        await charge_parameter_discovery.process_message(
            message=get_charge_parameter_discovery_req_message_no_departure_time()
        )
        assert (
            charge_parameter_discovery.message.body.charge_parameter_discovery_res
            is not None
        )
        charge_parameter_discovery_res = (
            charge_parameter_discovery.message.body.charge_parameter_discovery_res
        )
        assert charge_parameter_discovery_res.sa_schedule_list is not None
        sa_schedule_tuples = (
            charge_parameter_discovery_res.sa_schedule_list.schedule_tuples
        )

        for schedule_tuples in sa_schedule_tuples:
            schedule_duration = 0
            if schedule_tuples.p_max_schedule.schedule_entries is not None:
                first_entry_start_time = (
                    schedule_tuples.p_max_schedule.schedule_entries[
                        0
                    ].time_interval.start
                )
                last_entry_start_time = schedule_tuples.p_max_schedule.schedule_entries[
                    -1
                ].time_interval.start
                last_entry_schedule_duration = (
                    schedule_tuples.p_max_schedule.schedule_entries[
                        -1
                    ].time_interval.duration
                )
                schedule_duration = (
                    last_entry_start_time - first_entry_start_time
                ) + last_entry_schedule_duration
            assert schedule_duration >= twenty_four_hours_in_seconds

    async def test_charge_parameter_discovery_res_v2g2_761(self):
        # V2G2-761: If departure time was not provided, then SECC shall assume
        # that the EV intends to start charging without any delay
        charge_parameter_discovery = ChargeParameterDiscovery(self.comm_session)
        await charge_parameter_discovery.process_message(
            message=get_charge_parameter_discovery_req_message_no_departure_time()
        )
        assert (
            charge_parameter_discovery.message.body.charge_parameter_discovery_res
            is not None
        )
        charge_parameter_discovery_res = (
            charge_parameter_discovery.message.body.charge_parameter_discovery_res
        )

        assert charge_parameter_discovery_res.sa_schedule_list is not None
        sa_schedule_tuples = (
            charge_parameter_discovery_res.sa_schedule_list.schedule_tuples
        )

        for schedule_tuples in sa_schedule_tuples:
            assert schedule_tuples.p_max_schedule is not None
            found_entry_indicating_start_without_delay = False
            for entry in schedule_tuples.p_max_schedule.schedule_entries:
                if entry.time_interval.start == 0:
                    found_entry_indicating_start_without_delay = True
                    break

            assert found_entry_indicating_start_without_delay is True

    async def test_power_delivery_set_hlc_charging(
        self,
    ):

        power_delivery = PowerDelivery(self.comm_session)
        self.comm_session.evse_controller.set_hlc_charging = AsyncMock()

        # hlc is set to True
        await power_delivery.process_message(
            message=get_dummy_v2g_message_power_delivery_req_charge_start()
        )

        self.comm_session.evse_controller.set_hlc_charging.assert_called_with(True)

        # hlc is set to False
        await power_delivery.process_message(
            message=get_dummy_v2g_message_power_delivery_req_charge_stop()
        )

        self.comm_session.evse_controller.set_hlc_charging.assert_called_with(False)

    @pytest.mark.parametrize(
        "get_state_return_value, expected_next_state",
        [
            (CpState.B1, Terminate),
            (CpState.B2, Terminate),
            (CpState.C1, Terminate),
            (CpState.C2, CurrentDemand),
            (CpState.D1, Terminate),
            (CpState.D2, CurrentDemand),
        ],
    )
    async def test_power_delivery_state_c(
        self, get_state_return_value, expected_next_state
    ):

        power_delivery = PowerDelivery(self.comm_session)
        mock_get_cp_state = AsyncMock(return_value=get_state_return_value)
        self.comm_session.evse_controller.get_cp_state = mock_get_cp_state
        await power_delivery.process_message(
            message=get_dummy_v2g_message_power_delivery_req_charge_start()
        )
        assert power_delivery.next_state is expected_next_state

    async def test_service_discovery_req_unexpected_state(self):
        self.comm_session.selected_auth_option = AuthEnum.PNC_V2
        self.comm_session.config.free_charging_service = False
        service_discovery = ServiceDiscovery(self.comm_session)
        await service_discovery.process_message(
            message=get_dummy_v2g_message_service_discovery_req()
        )
        await service_discovery.process_message(
            message=get_dummy_v2g_message_service_discovery_req()
        )
        assert service_discovery.next_state is Terminate
        assert (
            service_discovery.message.body.service_discovery_res.response_code
            is ResponseCode.FAILED_SEQUENCE_ERROR
        )

    async def test_charging_status_evse_status(self):
        charging_status = ChargingStatus(self.comm_session)
        self.comm_session.selected_schedule = 1
        await charging_status.process_message(message=get_dummy_charging_status_req())

        charging_status_res = charging_status.message.body.charging_status_res
        assert charging_status_res.ac_evse_status == ACEVSEStatus(
            notification_max_delay=0,
            evse_notification=EVSENotification.NONE,
            rcd=False,
        )

    async def test_charging_status_evse_status_altered(self):
        charging_status = ChargingStatus(self.comm_session)
        self.comm_session.selected_schedule = 1

        async def get_ac_evse_status_patch():
            return ACEVSEStatus(
                notification_max_delay=0,
                evse_notification=EVSENotification.NONE,
                rcd=True,
            )

        self.comm_session.evse_controller.get_ac_evse_status = get_ac_evse_status_patch
        await charging_status.process_message(message=get_dummy_charging_status_req())
        charging_status_res = charging_status.message.body.charging_status_res
        assert charging_status_res.ac_evse_status == await get_ac_evse_status_patch()

    @pytest.mark.parametrize(
        "charging_session, expected_next_state",
        [
            (ChargingSession.PAUSE, Pause),
            (ChargingSession.TERMINATE, Terminate),
        ],
    )
    async def test_session_stop_req(self, charging_session, expected_next_state):
        # V2G2-718
        session_stop = SessionStop(self.comm_session)
        await session_stop.process_message(
            message=get_dummy_v2g_session_stop_req(charging_session)
        )
        assert session_stop.next_state == expected_next_state

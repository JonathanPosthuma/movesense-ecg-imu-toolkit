// Winlogger.cpp
#include "movesense.h"

#include "winlogger.h"
#include "whiteboard/ResourceClient.h"
#include "common/core/debug.h"
#include "oswrapper/thread.h"

// Bluetooth resources
#include "comm_ble_gattsvc/resources.h"
#include "comm_ble/resources.h"

#include "mem_logbook/resources.h"
#include "meas_temp/resources.h"
#include "movesense_time/resources.h"
#include "system_mode/resources.h"


// Measurement resources
#include "meas_acc/resources.h"
#include "meas_gyro/resources.h"
#include "meas_magn/resources.h"
#include "meas_imu/resources.h"
#include "meas_ecg/resources.h"
#include "meas_hr/resources.h"
#include "sbem-code/sbem_definitions.h"

// Memory resources
#include "mem_datalogger/resources.h"
#include "system_states/resources.h"
#include "ui_ind/resources.h"

// Led and timer resources
#include "component_max3000x/resources.h"
#include "component_led/resources.h"

const char* const winlogger::LAUNCHABLE_NAME = "winlogger";
constexpr wb::ExecutionContextId MY_EXECUTION_CONTEXT = WB_EXEC_CTX_APPLICATION;

// UUIDs for GATT service and characteristics
constexpr uint8_t SENSOR_DATASERVICE_UUID[] = { 0xf0, 0xe8, 0x50, 0x70, 0x0e, 0x63, 0x31, 0xb4, 0x5d, 0x4d, 0x85, 0x71, 0x52, 0x22, 0x80, 0x34 };
constexpr uint8_t COMMAND_CHAR_UUID[] = { 0xf0, 0xe8, 0x50, 0x70, 0x0e, 0x63, 0x31, 0xb4, 0x5d, 0x4d, 0x85, 0x71, 0x01, 0x00, 0x80, 0x34 };
constexpr uint16_t commandCharUUID16 = 0x0001;
constexpr uint8_t DATA_CHAR_UUID[] = { 0xf0, 0xe8, 0x50, 0x70, 0x0e, 0x63, 0x31, 0xb4, 0x5d, 0x4d, 0x85, 0x71, 0x02, 0x00, 0x80, 0x34 };
constexpr uint16_t dataCharUUID16 = 0x0002;

winlogger::winlogger():
    ResourceClient(WBDEBUG_NAME(__FUNCTION__), MY_EXECUTION_CONTEXT),
    LaunchableModule(LAUNCHABLE_NAME, MY_EXECUTION_CONTEXT),
    mCommandCharResource(wb::ID_INVALID_RESOURCE),
    mDataCharResource(wb::ID_INVALID_RESOURCE),
    mLeadsConnected(false),
    mIsLogging(false),
    mSensorSvcHandle(0),
    mCommandCharHandle(0),
    mStateCheckTimer(wb::ID_INVALID_TIMER),
    mStartLoggingTimer(wb::ID_INVALID_TIMER),
    mDisconnectElapsedTime(wb::ID_INVALID_TIMER),
    mNotificationsEnabled(false),
    mLogIdToFetch(0),
    mLogFetchOffset(0),
    mLogFetchReference(0),
    mDataLoggerState(WB_RES::DataLoggerStateValues::DATALOGGER_INVALID),
    mDataCharHandle(0),
    mDisconnectCounter(0),
    mDataloggerStopRequested(false)
{    
}

winlogger::~winlogger()
{
}


bool winlogger::initModule()
{
    mModuleState = WB_RES::ModuleStateValues::INITIALIZED;
    return true;
}

void winlogger::deinitModule()
{
    mModuleState = WB_RES::ModuleStateValues::UNINITIALIZED;
}

bool winlogger::startModule()
{
    mModuleState = WB_RES::ModuleStateValues::STARTED;

    // Subscribe to BLE connection status
    asyncSubscribe(WB_RES::LOCAL::COMM_BLE_PEERS());

    // Subscribe to system states
    asyncSubscribe(WB_RES::LOCAL::SYSTEM_STATES_STATEID(), AsyncRequestOptions::Empty, WB_RES::StateIdValues::CONNECTOR);

    // Configure custom GATT service
    configGattSvc();

    // Start the state check timer to monitor leads and logging status
    mStateCheckTimer = startTimer(LED_BLINKING_PERIOD, true);

    return true;
}

void winlogger::stopModule()
{
    asyncUnsubscribe(WB_RES::LOCAL::COMM_BLE_PEERS());
    asyncUnsubscribe(WB_RES::LOCAL::SYSTEM_STATES_STATEID(), AsyncRequestOptions::Empty, WB_RES::StateIdValues::CONNECTOR);

    // Stop the timers
    stopTimer(mStateCheckTimer);
    mStateCheckTimer = wb::ID_INVALID_TIMER;

    mModuleState = WB_RES::ModuleStateValues::STOPPED;
}

// Setup customGATTservice
void winlogger::configGattSvc()
{
    // Create custom GATT service
    WB_RES::GattSvc customGattSvc;
    WB_RES::GattChar characteristics[2];

    // Define the characteristics (data, command)
    WB_RES::GattChar& commandChar = characteristics[0];
    WB_RES::GattChar& dataChar = characteristics[1];

    // GATT properties
    WB_RES::GattProperty dataCharProp = WB_RES::GattProperty::NOTIFY;
    WB_RES::GattProperty commandCharProp = WB_RES::GattProperty::WRITE;

    dataChar.props = wb::MakeArray<WB_RES::GattProperty>( &dataCharProp, 1);
    dataChar.uuid = wb::MakeArray<uint8_t>( reinterpret_cast<const uint8_t*>(&DATA_CHAR_UUID), sizeof(DATA_CHAR_UUID));

    commandChar.props = wb::MakeArray<WB_RES::GattProperty>( &commandCharProp, 1);
    commandChar.uuid = wb::MakeArray<uint8_t>( reinterpret_cast<const uint8_t*>(&COMMAND_CHAR_UUID), sizeof(COMMAND_CHAR_UUID));

    // Combine chars to service
    customGattSvc.uuid = wb::MakeArray<uint8_t>( SENSOR_DATASERVICE_UUID, sizeof(SENSOR_DATASERVICE_UUID));
    customGattSvc.chars = wb::MakeArray<WB_RES::GattChar>(characteristics, 2);

    // Post the GATT service
    asyncPost(WB_RES::LOCAL::COMM_BLE_GATTSVC(), AsyncRequestOptions(NULL, 0, true), customGattSvc);
}
 

// Commands and enum definitions for GATT service
enum Commands
{
    HELLO           = 0,
    SUBSCRIBE       = 1,
    UNSUBSCRIBE     = 2,
    FETCH_LOG       = 3, // Command to fetch and send offline data
    INIT_OFFLINE    = 4,
    GET_LOG_COUNT   = 5, // New command to request the number of logs
    STOP_LOGGING    = 6  // <— newly added
};

enum responses
{
    COMMAND_RESULT = 1,
    DATA = 2,       // Sending data as part of the response
    DATA_PART2 = 3, // Continuing if the data doesn't fit in one BLE packet
    DATA_PART3 = 4,
};

winlogger::DataSub* winlogger::findDataSub(const wb::LocalResourceId localResourceId)
{
    for (size_t i=0; i<MAX_DATASUB_COUNT; i++)
    {
        const DataSub &ds= mDataSubs[i];
        if (ds.resourceId.localResourceId == localResourceId)
            return &(mDataSubs[i]);
    }
    return nullptr;
}

winlogger::DataSub* winlogger::findDataSub(const wb::ResourceId resourceId)
{
    for (size_t i=0; i<MAX_DATASUB_COUNT; i++)
    {
        const DataSub &ds= mDataSubs[i];
        if (ds.resourceId == resourceId)
            return &(mDataSubs[i]);
    }
    return nullptr;
}

winlogger::DataSub*  winlogger::findDataSubByRef(const uint8_t clientReference)
{
    for (size_t i=0; i<MAX_DATASUB_COUNT; i++)
    {
        const DataSub &ds= mDataSubs[i];
        if (ds.clientReference == clientReference)
            return &(mDataSubs[i]);
    }
    return nullptr;
}

winlogger::DataSub* winlogger::getFreeDataSubSlot()
{
    for (size_t i=0; i<MAX_DATASUB_COUNT; i++)
    {
        const DataSub &ds= mDataSubs[i];
        if (ds.clientReference == 0 && ds.resourceId == wb::ID_INVALID_RESOURCE)
            return &(mDataSubs[i]);
    }
    return nullptr;
}

void winlogger::handleIncomingCommand(const wb::Array<uint8> &commandData){
    uint8_t cmd       = commandData[0];
    uint8_t reference = commandData[1];
    const uint8_t *pData   = commandData.size()>2 ? &(commandData[2]) : nullptr;
    uint16_t dataLen       = commandData.size() - 2;

    switch (cmd)
    {
        case Commands::HELLO:
        {
            DEBUGLOG("HELLO command received. Initiating power-down sequence.");
        
            // Clean offline storage: Clear the logbook by sending a DELETE request.
            asyncDelete(WB_RES::LOCAL::MEM_LOGBOOK_ENTRIES());
        
            // Send a power-down response to the client.
            uint8_t powerMsg[] = { COMMAND_RESULT, reference, 'P', 'O', 'W', 'E', 'R' };
            WB_RES::Characteristic dataCharValue;
            dataCharValue.bytes = wb::MakeArray<uint8_t>(powerMsg, sizeof(powerMsg));
            asyncPut(mDataCharResource, AsyncRequestOptions::Empty, dataCharValue);
        
            // Immediately clear LED indications.
            asyncPut(WB_RES::LOCAL::UI_IND_VISUAL(), AsyncRequestOptions::Empty,
                     WB_RES::VisualIndTypeValues::NO_VISUAL_INDICATIONS);
        
            // Mark that logging is stopped.
            mDataloggerStopRequested = true;
            mIsLogging = false;
        
            // Directly issue the wakeup command.
            asyncPut(WB_RES::LOCAL::COMPONENT_MAX3000X_WAKEUP(), AsyncRequestOptions::ForceAsync, (uint8_t)1);
        
            // Immediately send the system mode command to enter full power-off.
            asyncPut(WB_RES::LOCAL::SYSTEM_MODE(), AsyncRequestOptions(NULL, 0, true),
                     WB_RES::SystemModeValues::FULLPOWEROFF);
        
            return;
        }

        case Commands::SUBSCRIBE:
        {
            DataSub *pDataSub = getFreeDataSubSlot();
            if (!pDataSub)
            {
                DEBUGLOG("No free datasub slot");
                // 507: HTTP_CODE_INSUFFICIENT_STORAGE
                uint8_t errorMsg[] = { COMMAND_RESULT, reference, 0x01, 0xFB };
                WB_RES::Characteristic dataCharValue;
                dataCharValue.bytes = wb::MakeArray<uint8_t>(errorMsg, sizeof(errorMsg));
                asyncPut(mDataCharResource, AsyncRequestOptions(NULL, 0, true), dataCharValue);
                return;
            }

            // Store client reference to array and trigger subscribe
            DataSub &dataSub = *pDataSub;
            char pathBuffer[160] = {0}; // Big enough since MTU is 161
            memcpy(pathBuffer, pData, dataLen);

            dataSub.subStarted    = true;
            dataSub.subCompleted  = false;
            dataSub.clientReference = reference;
            getResource(pathBuffer, dataSub.resourceId);
            asyncSubscribe(dataSub.resourceId, AsyncRequestOptions::ForceAsync);
        }
        break;

        case Commands::FETCH_LOG:
        {
            ASSERT(pData != nullptr);
            ASSERT(dataLen == sizeof(uint32_t));
            memcpy(&mLogIdToFetch, pData, dataLen);
            mLogFetchReference = reference;
            asyncGet(WB_RES::LOCAL::MEM_LOGBOOK_BYID_LOGID_DATA(), AsyncRequestOptions::ForceAsync, mLogIdToFetch);
        }
        break;

        case Commands::UNSUBSCRIBE:
        {
            DEBUGLOG("Commands::UNSUBSCRIBE. reference: %d", reference);
            DataSub *pDataSub = findDataSubByRef(reference);
            if (pDataSub)
            {
                asyncUnsubscribe(pDataSub->resourceId);
                pDataSub->resourceId    = wb::ID_INVALID_RESOURCE;
                pDataSub->clientReference = 0;
            }
        }
        break;

        case Commands::STOP_LOGGING:
        {
            DEBUGLOG("STOP_LOGGING command received. Calling stopLogging().");

            // Delegate to your helper function
            stopLogging();

            // Send back an ACK (COMMAND_RESULT, no-error code)
            uint8_t resp[] = { COMMAND_RESULT, reference, 0x00 };
            WB_RES::Characteristic dataCharValue;
            dataCharValue.bytes = wb::MakeArray<uint8_t>(resp, sizeof(resp));
            asyncPut(mDataCharResource, AsyncRequestOptions::Empty, dataCharValue);

            return;
        }

        // you can add more commands here…

        default:
            DEBUGLOG("Unknown command: %d", cmd);
            break;
    }
}

void winlogger::onGetResult(wb::RequestId requestId,
                                      wb::ResourceId resourceId,
                                      wb::Result resultCode,
                                      const wb::Value& rResultData)
{
    DEBUGLOG("winlogger::onGetResult");
    switch(resourceId.localResourceId)
    {
        case WB_RES::LOCAL::MEM_DATALOGGER_STATE::LID:
        {
            WB_RES::DataLoggerState dlState = rResultData.convertTo<WB_RES::DataLoggerState>();
            mDataLoggerState = dlState;
            break;
        }
        case WB_RES::LOCAL::COMM_BLE_GATTSVC_SVCHANDLE::LID:
        {
            // This code finalizes the service setup (triggered by code in onPostResult)
            const WB_RES::GattSvc &svc = rResultData.convertTo<const WB_RES::GattSvc &>();
            for (size_t i=0; i<svc.chars.size(); i++)
            {
                // Find out characteristic handles and store them for later use
                const WB_RES::GattChar &c = svc.chars[i];
                // Extract 16 bit sub-uuid from full 128bit uuid
                DEBUGLOG("c.uuid.size(): %u", c.uuid.size());
                uint16_t uuid16 = *reinterpret_cast<const uint16_t*>(&(c.uuid[12]));
                
                DEBUGLOG("char[%u] uuid16: 0x%04X", i, uuid16);

                if(uuid16 == dataCharUUID16)
                    mDataCharHandle = c.handle.hasValue() ? c.handle.getValue() : 0;
                else if(uuid16 == commandCharUUID16)
                    mCommandCharHandle = c.handle.hasValue() ? c.handle.getValue() : 0;
            }

            if (!mCommandCharHandle || !mDataCharHandle)
            {
                DEBUGLOG("ERROR: Not all chars were configured!");
                return;
            }

            char pathBuffer[32]= {'\0'};
            snprintf(pathBuffer, sizeof(pathBuffer), "/Comm/Ble/GattSvc/%d/%d", mSensorSvcHandle, mCommandCharHandle);
            getResource(pathBuffer, mCommandCharResource);
            snprintf(pathBuffer, sizeof(pathBuffer), "/Comm/Ble/GattSvc/%d/%d", mSensorSvcHandle, mDataCharHandle);
            getResource(pathBuffer, mDataCharResource);

            // Forse subscriptions asynchronously to save stack (will have stack overflow if not) 
            // Subscribe to listen to intervalChar notifications (someone writes new value to intervalChar) 
            asyncSubscribe(mCommandCharResource, AsyncRequestOptions(NULL, 0, true));
            // Subscribe to listen to measChar notifications (someone enables/disables the INDICATE characteristic) 
            asyncSubscribe(mDataCharResource,  AsyncRequestOptions(NULL, 0, true));
            break;
        }

        case WB_RES::LOCAL::MEM_LOGBOOK_BYID_LOGID_DATA::LID:
        {
            const auto &stream = rResultData.convertTo<const wb::ByteStream &>();
            DEBUGLOG("MEM_LOGBOOK_BYID_LOGID_DATA. resultCode: %d", resultCode);
            if (resultCode >= 400)
            {
                // Don't do a thing...
                return;
            }

            DEBUGLOG("Sendind from get. size: %d", stream.length());

            handleSendingLogbookData(stream.data, stream.length());
            if (resultCode == wb::HTTP_CODE_CONTINUE)
            {
                // Do another GET request to get the next bytes (needs to be async)
                asyncGet(WB_RES::LOCAL::MEM_LOGBOOK_BYID_LOGID_DATA(), AsyncRequestOptions::ForceAsync, mLogIdToFetch);
            }
            if (resultCode == wb::HTTP_CODE_OK)
            {
                DEBUGLOG("Fetching log complete. sending end marker.");
                // Send end marker (offset and no bytes)
                handleSendingLogbookData(nullptr, 0);
                // Mark "no current log"
                mLogIdToFetch=0;
                mLogFetchOffset=0;
                mLogFetchReference=0;
            }
            break;
        }
    }
}

void winlogger::handleSendingLogbookData(const uint8_t *pData, uint32_t length)
{

    // Forward data to client in same format (offset + bytes)
    // If length > 150, split in two notifications
    memset(mDataMsgBuffer, 0, sizeof(mDataMsgBuffer));
    mDataMsgBuffer[0] = DATA;
    mDataMsgBuffer[1] = mLogFetchReference;
    
    // Copy offset
    size_t writePos = 2;
    memcpy(&(mDataMsgBuffer[writePos]), &mLogFetchOffset, sizeof(mLogFetchOffset));
    writePos += sizeof(mLogFetchOffset);

    size_t firstPartLen = (length>150) ? 150 : length;
    size_t secondPartLen = (length == firstPartLen) ? 0 : length - firstPartLen;
    DEBUGLOG("firstPartLen: %d, secondPartLen: %d", firstPartLen, secondPartLen);

    if (firstPartLen > 0)
    {
        memcpy(&(mDataMsgBuffer[writePos]), pData, firstPartLen);
        writePos += firstPartLen;
        mLogFetchOffset += firstPartLen;
    }
    else
    {
        DEBUGLOG("End of file marker");
    }

    WB_RES::Characteristic dataCharValue;
    dataCharValue.bytes = wb::MakeArray<uint8_t>(mDataMsgBuffer, writePos);
    asyncPut(mDataCharResource, AsyncRequestOptions::Empty, dataCharValue);

    if (secondPartLen > 0)
    {
        mDataMsgBuffer[0] = DATA_PART2;

        // Calc and write second offset
        writePos = 2;
        memcpy(&(mDataMsgBuffer[writePos]), &mLogFetchOffset, sizeof(mLogFetchOffset));
        writePos += sizeof(mLogFetchOffset);
        // Copy second part data
        memcpy(&(mDataMsgBuffer[writePos]), &(pData[firstPartLen]), secondPartLen);
        writePos += secondPartLen;
        mLogFetchOffset += secondPartLen;

        dataCharValue.bytes = wb::MakeArray<uint8_t>(mDataMsgBuffer, writePos);
        asyncPut(mDataCharResource, AsyncRequestOptions::Empty, dataCharValue);
    }
}

void winlogger::unsubscribeAllStreams()
{
    for (size_t i=0;i<MAX_DATASUB_COUNT; i++)
    {
        if (mDataSubs[i].resourceId != wb::ID_INVALID_RESOURCE)
        {
            asyncUnsubscribe(mDataSubs[i].resourceId);
            mDataSubs[i].clientReference = 0;
            mDataSubs[i].resourceId = wb::ID_INVALID_RESOURCE;
            mDataSubs[i].subStarted = false;
            mDataSubs[i].subCompleted = false;
        }
    }
}

/** @see whiteboard::ResourceClient::onGetResult */
void  winlogger::onSubscribeResult(wb::RequestId requestId,
                                              wb::ResourceId resourceId,
                                              wb::Result resultCode,
                                              const wb::Value& rResultData)
{
    DEBUGLOG("onSubscribeResult() resourceId: %u, resultCode: %d", resourceId, resultCode);

    switch (resourceId.localResourceId)
    {
        case WB_RES::LOCAL::COMM_BLE_PEERS::LID:
            {
                DEBUGLOG("OnSubscribeResult: WB_RES::LOCAL::COMM_BLE_PEERS: %d", resultCode);
                return;
            }
            break;
        case WB_RES::LOCAL::COMM_BLE_GATTSVC_SVCHANDLE_CHARHANDLE::LID:
            {
                DEBUGLOG("OnSubscribeResult: COMM_BLE_GATTSVC*: %d", resultCode);
                return;
            }
            break;
        default:
        {
            // All other notifications. These must be the client subscribed data streams
            winlogger::DataSub *ds = findDataSub(resourceId);
            if (ds == nullptr)
            {
                DEBUGLOG("DataSub not found for resource: %u", resourceId);
                return;
            }
            ASSERT(ds->subStarted);
            if (ds->subCompleted)
            {
                DEBUGLOG("subCompleted already: %u", resourceId);
                return;
            }

            if (resultCode >= 400)
            {
                ds->clientReference = 0;
                ds->resourceId = wb::ID_INVALID_RESOURCE;
                ds->subStarted=false;
                ds->subCompleted=false;
            }
            else
            {
                ds->subCompleted=true;
            }
        }
        break;
    }
}
void winlogger::onNotify(wb::ResourceId resourceId,
                         const wb::Value& value,
                         const wb::ParameterList& rParameters)
{
    switch(resourceId.localResourceId)
    {
        case WB_RES::LOCAL::COMM_BLE_PEERS::LID:
        {
            WB_RES::PeerChange peerChange = value.convertTo<WB_RES::PeerChange>();
            if (peerChange.state == peerChange.state.DISCONNECTED)
            {
                // If connection is dropped, unsubscribe all data streams so that the sensor does not stay on for no reason
                unsubscribeAllStreams();
            }
            break;
        }

        case WB_RES::LOCAL::SYSTEM_STATES_STATEID::LID:
        {
            WB_RES::StateChange stateChange = value.convertTo<WB_RES::StateChange>(); 
            if (stateChange.stateId == WB_RES::StateIdValues::CONNECTOR)
            {
                DEBUGLOG("Lead state updated. newState: %d", stateChange.newState);
                mLeadsConnected = (stateChange.newState != 0);

                if (mLeadsConnected && !mIsLogging)
                {
                    // Start logging if leads are connected and we are not logging
                    startLogging();
                }
                else if (!mLeadsConnected && mIsLogging)
                {
                    // Reset the counter when we first detect disconnection
                    mDisconnectCounter = 0;
                }
            } 
            break;
        }

        case WB_RES::LOCAL::COMM_BLE_GATTSVC_SVCHANDLE_CHARHANDLE::LID:
        {
            WB_RES::LOCAL::COMM_BLE_GATTSVC_SVCHANDLE_CHARHANDLE::SUBSCRIBE::ParameterListRef parameterRef(rParameters);
            if (parameterRef.getCharHandle() == mCommandCharHandle)
            {
                const WB_RES::Characteristic &charValue = value.convertTo<const WB_RES::Characteristic &>();

                DEBUGLOG("onNotify: mCommandCharHandle: len: %d", charValue.bytes.size());

                handleIncomingCommand(charValue.bytes);
                return;
            }
            else if (parameterRef.getCharHandle() == mDataCharHandle)
            {
                const WB_RES::Characteristic &charValue = value.convertTo<const WB_RES::Characteristic &>();
                // Update the notification state so we know if to forward data to datapipe
                mNotificationsEnabled = charValue.notifications.hasValue() ? charValue.notifications.getValue() : false;
                DEBUGLOG("onNotify: mDataCharHandle. mNotificationsEnabled: %d", mNotificationsEnabled);
            }
            break;
        }

        case WB_RES::LOCAL::MEM_LOGBOOK_BYID_LOGID_DATA::LID:
        {
            winlogger::DataSub *ds = findDataSub(resourceId.localResourceId);
            if (ds == nullptr)
            {
                DEBUGLOG("DataSub not found for resource: %u", resourceId);
                return;
            }

            // Handle special case of subscribing logbook data
            const auto &dataNotification = value.convertTo<const WB_RES::LogDataNotification &>();
            const size_t length = dataNotification.bytes.size();
            DEBUGLOG("Logbook data notification. offset: %d, length: %d", dataNotification.offset, length);

            // Forward data to client in the same format (offset + bytes)
            // If length > 150, split into two notifications
            memset(mDataMsgBuffer, 0, sizeof(mDataMsgBuffer));
            mDataMsgBuffer[0] = DATA;
            mDataMsgBuffer[1] = ds->clientReference;

            // Copy offset
            size_t writePos = 2;
            memcpy(&(mDataMsgBuffer[writePos]), &(dataNotification.offset), sizeof(dataNotification.offset));
            writePos += sizeof(dataNotification.offset);
            size_t firstPartLen = (length > 150) ? 150 : length;
            size_t secondPartLen = (length == firstPartLen) ? 0 : length - firstPartLen;
            DEBUGLOG("firstPartLen: %d, secondPartLen: %d", firstPartLen, secondPartLen);

            if (firstPartLen > 0)
            {
                memcpy(&(mDataMsgBuffer[writePos]), &(dataNotification.bytes[0]), firstPartLen);
                writePos += firstPartLen;
            }
            else
            {
                DEBUGLOG("End of file marker");
            }

            WB_RES::Characteristic dataCharValue;
            dataCharValue.bytes = wb::MakeArray<uint8_t>(mDataMsgBuffer, writePos);
            asyncPut(mDataCharResource, AsyncRequestOptions::Empty, dataCharValue);

            if (secondPartLen > 0)
            {
                mDataMsgBuffer[0] = DATA_PART2;

                // Calculate and write second offset
                writePos = 2;
                uint32_t secondOffset = dataNotification.offset + firstPartLen;
                memcpy(&(mDataMsgBuffer[writePos]), &secondOffset, sizeof(secondOffset));
                writePos += sizeof(secondOffset);
                // Copy second part data
                memcpy(&(mDataMsgBuffer[writePos]), &(dataNotification.bytes[firstPartLen]), secondPartLen);
                writePos += secondPartLen;

                dataCharValue.bytes = wb::MakeArray<uint8_t>(mDataMsgBuffer, writePos);
                asyncPut(mDataCharResource, AsyncRequestOptions::Empty, dataCharValue);
            }
            break;
        }

        default:
        {
            // All other notifications. These must be the client-subscribed data streams
            winlogger::DataSub *ds = findDataSub(resourceId);
            if (ds == nullptr)
            {
                DEBUGLOG("DataSub not found for resource: %u", resourceId);
                return;
            }

            DEBUGLOG("DS clientReference: %u", ds->clientReference);
            DEBUGLOG("DS subStarted: %u", ds->subStarted);
            DEBUGLOG("DS subCompleted: %u", ds->subCompleted);

            // Make sure we can serialize the data
            size_t length = getSbemLength(resourceId.localResourceId, value);
            if (length == 0)
            {
                DEBUGLOG("No length for localResourceId: %u", resourceId.localResourceId);
                return;
            }

            // Forward data to client
            memset(mDataMsgBuffer, 0, sizeof(mDataMsgBuffer));
            mDataMsgBuffer[0] = DATA;
            mDataMsgBuffer[1] = ds->clientReference;

            size_t writePos = 2;
            size_t firstPartLen = (length > 150) ? 150 : length;
            size_t secondPartLen = (length == firstPartLen) ? 0 : length - firstPartLen;
            DEBUGLOG("firstPartLen: %d, secondPartLen: %d", firstPartLen, secondPartLen);

            // Write the first part of the notification value
            length = writeToSbemBuffer(&mDataMsgBuffer[2], sizeof(mDataMsgBuffer) - 2, 0, resourceId.localResourceId, value);
            writePos += firstPartLen;

            WB_RES::Characteristic dataCharValue;
            dataCharValue.bytes = wb::MakeArray<uint8_t>(mDataMsgBuffer, writePos);
            asyncPut(mDataCharResource, AsyncRequestOptions::Empty, dataCharValue);

            if (secondPartLen > 0)
            {
                mDataMsgBuffer[0] = DATA_PART2;
                writePos = 2;
                // Write the second part of data starting from offset "firstPartLen"
                length = writeToSbemBuffer(&mDataMsgBuffer[2], sizeof(mDataMsgBuffer) - 2, firstPartLen, resourceId.localResourceId, value);
                writePos += secondPartLen;
                // And send it
                dataCharValue.bytes = wb::MakeArray<uint8_t>(mDataMsgBuffer, writePos);
                asyncPut(mDataCharResource, AsyncRequestOptions::Empty, dataCharValue);
            }
            return;
        }
    }
}


void winlogger::onPutResult(wb::RequestId requestId, 
                                       wb::ResourceId resourceId, 
                                       wb::Result resultCode, 
                                       const wb::Value& rResultData)
{
    DEBUGLOG("winlogger::onPutResult: %d", resultCode);

    switch (resourceId.localResourceId)
    {
        case WB_RES::LOCAL::MEM_DATALOGGER_STATE::LID:
        {
            if (resultCode == wb::HTTP_CODE_OK)
            {
                // Datalogger state was changed successfully
                DEBUGLOG("Datalogger state changed. mDataloggerStopRequested: ", mDataloggerStopRequested);
                if (mDataloggerStopRequested)
                {
                    // Configure wakeup. Device put to sleep in onPutResult
                    asyncPut(WB_RES::LOCAL::COMPONENT_MAX3000X_WAKEUP(), AsyncRequestOptions::ForceAsync, (uint8_t)1);
                }
            }
            break;
        }
        case WB_RES::LOCAL::COMPONENT_MAX3000X_WAKEUP::LID:
        {
            if (resultCode == wb::HTTP_CODE_OK)
            {
                // Wakeup configured, put to poweroff
                DEBUGLOG("Wakeup configured, going power off");
                // Make PUT request to enter power off mode
                asyncPut(WB_RES::LOCAL::SYSTEM_MODE(),
                        AsyncRequestOptions(NULL, 0, true), // Force async
                        WB_RES::SystemModeValues::FULLPOWEROFF);
            }
            break;
        }
        case WB_RES::LOCAL::SYSTEM_MODE::LID:
        {
            if (resultCode == wb::HTTP_CODE_OK)
            {
                // Device is now in power off mode
                DEBUGLOG("Device is going to power off mode");
            }
            break;
        }
    }
}

void winlogger::onPostResult(wb::RequestId requestId, 
                                       wb::ResourceId resourceId, 
                                       wb::Result resultCode, 
                                       const wb::Value& rResultData)
{
    DEBUGLOG("winlogger::onPostResult: %d", resultCode);

    if (resultCode == wb::HTTP_CODE_CREATED)
    {
        // Custom Gatt service was created
        mSensorSvcHandle = (int32_t)rResultData.convertTo<uint16_t>();
        DEBUGLOG("Custom Gatt service was created. handle: %d", mSensorSvcHandle);
        
        // Request more info about created svc so we get the char handles
        asyncGet(WB_RES::LOCAL::COMM_BLE_GATTSVC_SVCHANDLE(), AsyncRequestOptions(NULL,0,true), mSensorSvcHandle);
        // Note: The rest of the init is performed in onGetResult()
    }
}

void winlogger::startLogging()
{
    mDataloggerStopRequested = false;

    // If we are already logging or leads aren't connected, don't start again
    if (mIsLogging || !mLeadsConnected)
    {
        return;
    }

    // Set mIsLogging to true right away
    // This prevents another call to startLogging() before we finish setup
    mIsLogging = true;

    DEBUGLOG("Starting ECG + ACC logging. Leads connected: %d, BLE connected: %d", mLeadsConnected, mBleConnected);

    // Create data entries for both ECG and Accelerometer
    WB_RES::DataLoggerConfig ldConfig;
    WB_RES::DataEntry entries[2];

    // ECG data entry (200 Hz ECG resource)
    entries[0].path = "/Meas/ECG/200/mV";
    
    // IMU data entry path (26 Hz IMU resource)
    entries[1].path = "/Meas/IMU6/26";


    ldConfig.dataEntries.dataEntry = wb::MakeArray<WB_RES::DataEntry>(entries, 2);

    // Start the logging process
    asyncPut(WB_RES::LOCAL::MEM_DATALOGGER_CONFIG(), AsyncRequestOptions::ForceAsync, ldConfig);
    asyncPut(WB_RES::LOCAL::MEM_DATALOGGER_STATE(), AsyncRequestOptions::ForceAsync, WB_RES::DataLoggerStateValues::DATALOGGER_LOGGING);

    DEBUGLOG("ECG + ACC logging started. mIsLogging: %d", mIsLogging);

    // Visual indication of logging start
    asyncPut(WB_RES::LOCAL::UI_IND_VISUAL(), AsyncRequestOptions::ForceAsync,
             WB_RES::VisualIndTypeValues::CONTINUOUS_VISUAL_INDICATION);

    // Start the timer to stop the LED blinking after 3 seconds
    mStartLoggingTimer = startTimer(LED_START_LOGGING_BLINKING_TIMEOUT, false);
}

void winlogger::stopLogging()
{
    if (!mIsLogging)
        return; // Not logging, nothing to stop

    DEBUGLOG("Stopping logging...");

    // Turn off visual indications when logging stops (just in case)
    asyncPut(WB_RES::LOCAL::UI_IND_VISUAL(), AsyncRequestOptions::Empty,
             WB_RES::VisualIndTypeValues::NO_VISUAL_INDICATIONS);

    // Mark that we want to stop logging
    mDataloggerStopRequested = true;
    asyncPut(WB_RES::LOCAL::MEM_DATALOGGER_STATE(), AsyncRequestOptions::ForceAsync,
             WB_RES::DataLoggerStateValues::DATALOGGER_READY);

    mIsLogging = false;
}

void winlogger::onTimer(wb::TimerId timerId)
{
    // Check if this timer callback is for turning off the shutdown LED blink.
    if (timerId == mShutdownLedTimer)
    {
        mShutdownLedTimer = wb::ID_INVALID_TIMER;
        asyncPut(WB_RES::LOCAL::UI_IND_VISUAL(), AsyncRequestOptions::Empty,
                 WB_RES::VisualIndTypeValues::NO_VISUAL_INDICATIONS);
        return;
    }

    if (timerId == mStartLoggingTimer)
    {
        // Stop the start-logging LED indication after 3 seconds
        mStartLoggingTimer = wb::ID_INVALID_TIMER;
        asyncPut(WB_RES::LOCAL::UI_IND_VISUAL(), AsyncRequestOptions::Empty,
                 WB_RES::VisualIndTypeValues::NO_VISUAL_INDICATIONS);
        return;
    }

    STATIC_VERIFY(WB_EXEC_CTX_APPLICATION == WB_RES::LOCAL::MEM_DATALOGGER_STATE::EXECUTION_CONTEXT,
                  DataLogger_must_be_application_thread);
    asyncGet(WB_RES::LOCAL::MEM_DATALOGGER_STATE());

    // 1) Check if leads are disconnected while logging.
    if (!mLeadsConnected && mIsLogging)
    {
        mDisconnectCounter++;

        // If leads are disconnected for 9 hours, stop logging and trigger a blink.
        if (mDisconnectCounter * LED_BLINKING_PERIOD >= 32400000) // 166 minutes
        {
            DEBUGLOG("Leads disconnected for half minute. Stopping logging.");
            stopLogging();
            mDisconnectCounter = 0;

            // Blink LED once using the same mechanism as before:
            // Turn LED on (using CONTINUOUS_VISUAL_INDICATION) then schedule a timer to turn it off.
            asyncPut(WB_RES::LOCAL::UI_IND_VISUAL(), AsyncRequestOptions::Empty,
                     WB_RES::VisualIndTypeValues::CONTINUOUS_VISUAL_INDICATION);
            mShutdownLedTimer = startTimer(LED_START_LOGGING_BLINKING_TIMEOUT, false);
        }
    }
    else
    {
        // Reset counter if leads are reconnected or logging has stopped.
        mDisconnectCounter = 0;
    }

    // 2) If leads are connected or the datalogger is still running, skip further shutdown logic.
    if (mLeadsConnected || mDataLoggerState == WB_RES::DataLoggerStateValues::DATALOGGER_LOGGING)
    {
        DEBUGLOG("Leads connected [%d] or datalogger running [%d]. Postponing shutdown.",
                 mLeadsConnected, mDataLoggerState);
        return;
    }

    // 3) Otherwise, no further actions needed.
    DEBUGLOG("No leads, no logging, no further actions needed.");
    return;
}


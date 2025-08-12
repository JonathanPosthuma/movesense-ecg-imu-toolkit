// ECGIMULoggerApp.cpp
#include "movesense.h"
#include "ECGIMULoggerApp.h"
#include "common/core/debug.h"
#include "whiteboard/ResourceClient.h"
#include "whiteboard/builtinTypes/ByteStream.h"  // For ByteStream handling

#include "common/core/debug.h"
#include "sbem/Sbem.hpp"
#include "oswrapper/thread.h"

#include "comm_ble_gattsvc/resources.h"
#include "comm_ble/resources.h"
#include "meas_ecg/resources.h"
#include "meas_imu/resources.h"
#include "mem_datalogger/resources.h"
#include "mem_logbook/resources.h"
#include "meas_acc/resources.h"
#include "movesense_time/resources.h"
#include "system_states/resources.h"
#include "ui_ind/resources.h"

#include "SimpleQueue.h"  // Include the custom queue implementation
#include <stdint.h>  // For fixed-width integer types


// UUIDs for GATT service and characteristics
constexpr uint8_t SENSOR_DATASERVICE_UUID[] = { 0xf0, 0xe8, 0x50, 0x70, 0x0e, 0x63, 0x31, 0xb4, 0x5d, 0x4d, 0x85, 0x71, 0x52, 0x22, 0x80, 0x34 };
constexpr uint8_t COMMAND_CHAR_UUID[]       = { 0xf0, 0xe8, 0x50, 0x70, 0x0e, 0x63, 0x31, 0xb4, 0x5d, 0x4d, 0x85, 0x71, 0x01, 0x00, 0x80, 0x34 };
constexpr uint8_t DATA_CHAR_UUID[]          = { 0xf0, 0xe8, 0x50, 0x70, 0x0e, 0x63, 0x31, 0xb4, 0x5d, 0x4d, 0x85, 0x71, 0x02, 0x00, 0x80, 0x34 };

const char* const ECGIMULoggerApp::LAUNCHABLE_NAME = "ECGIMULoggerApp";

constexpr wb::ExecutionContextId MY_EXECUTION_CONTEXT = WB_EXEC_CTX_APPLICATION;

ECGIMULoggerApp::ECGIMULoggerApp()
    : ResourceClient(WBDEBUG_NAME("ECGIMULoggerApp"), WB_EXEC_CTX_APPLICATION),
      LaunchableModule(LAUNCHABLE_NAME, WB_EXEC_CTX_APPLICATION),
      mBleConnected(false),
      mIsLogging(false),
      mLeadsConnected(false),
      mSendBufferLength(0),
      mDataCharResource(0),
      mCommandCharResource(0),
      mFirstPacketSent(false),
      mLogSendReference(0),
      mSensorSvcHandle(0),
      mCurrentLogId(0),
      mIsFetchingLogData(false),
      mIsFirstDataPacket(false),
      mSendBuffer{0}
{
}

ECGIMULoggerApp::~ECGIMULoggerApp()
{
}

#define MAX_BLE_PACKET_SIZE 20   // Adjust based on your BLE setting

bool ECGIMULoggerApp::initModule()
{
    mModuleState = WB_RES::ModuleStateValues::INITIALIZED;
    return true;
}

void ECGIMULoggerApp::deinitModule()
{
    mModuleState = WB_RES::ModuleStateValues::UNINITIALIZED;
}

bool ECGIMULoggerApp::startModule()
{
    mModuleState = WB_RES::ModuleStateValues::STARTED;

    // Subscribe to BLE peer status
    asyncSubscribe(WB_RES::LOCAL::COMM_BLE_PEERS());

    // Subscribe to ECG leads status
    asyncSubscribe(WB_RES::LOCAL::SYSTEM_STATES_STATEID(), AsyncRequestOptions::Empty, WB_RES::StateIdValues::CONNECTOR);

    // Set up the GATT service for data transfer
    setupCustomGattService();

    return true;
}

void ECGIMULoggerApp::stopModule()
{
    // Unsubscribe from BLE peer status
    asyncUnsubscribe(WB_RES::LOCAL::COMM_BLE_PEERS());

    // Unsubscribe from ECG leads status
    asyncUnsubscribe(WB_RES::LOCAL::SYSTEM_STATES_STATEID(), AsyncRequestOptions::Empty, WB_RES::StateIdValues::CONNECTOR);

    mModuleState = WB_RES::ModuleStateValues::STOPPED;
}

// Commands and Responses Enum Definitions
enum Commands
{
    HELLO = 0,
    SUBSCRIBE = 1,
    UNSUBSCRIBE = 2,
    FETCH_OFFLINE_DATA = 3, // Command to fetch and send offline data
    INIT_OFFLINE = 4,
};

enum Responses
{
    COMMAND_RESULT = 1,
    DATA = 2,       // Sending data as part of the response
    DATA_PART2 = 3, // Continuing if the data doesn't fit in one BLE packet
    DATA_PART3 = 4,
};

// GATT Service Setup and Data Transfer
void ECGIMULoggerApp::setupCustomGattService()
{
    // Create custom GATT service
    WB_RES::GattSvc customGattSvc;
    WB_RES::GattChar characteristics[2];

    // Define the characteristics (data, command)
    WB_RES::GattChar& dataChar = characteristics[0];
    WB_RES::GattChar& commandChar = characteristics[1];

    // GATT properties
    WB_RES::GattProperty dataProp    = WB_RES::GattProperty::NOTIFY;
    WB_RES::GattProperty commandProp = WB_RES::GattProperty::WRITE;

    dataChar.props    = wb::MakeArray<WB_RES::GattProperty>(&dataProp, 1);
    commandChar.props = wb::MakeArray<WB_RES::GattProperty>(&commandProp, 1);

    // Assign UUIDs
    dataChar.uuid    = wb::MakeArray<uint8_t>(DATA_CHAR_UUID, sizeof(DATA_CHAR_UUID));
    commandChar.uuid = wb::MakeArray<uint8_t>(COMMAND_CHAR_UUID, sizeof(COMMAND_CHAR_UUID));

    // Setup GATT service with characteristics
    customGattSvc.uuid  = wb::MakeArray<uint8_t>(SENSOR_DATASERVICE_UUID, sizeof(SENSOR_DATASERVICE_UUID));
    customGattSvc.chars = wb::MakeArray<WB_RES::GattChar>(characteristics, 2);

    // Post the GATT service
    asyncPost(WB_RES::LOCAL::COMM_BLE_GATTSVC(), AsyncRequestOptions::Empty, customGattSvc);
}

// Member variables
SimpleQueue mLogIdsToSend; // Use SimpleQueue instead of std::queue

// Method to send offline data
void ECGIMULoggerApp::sendOfflineData(uint8_t reference)
{
    mLogSendReference = reference;

    // Clear the existing log IDs in the queue
    mLogIdsToSend.clear();

    // Reset flags
    mIsFetchingLogData = false;
    mCurrentLogId      = 0;

    // Get logbook entries to begin fetching data
    asyncGet(WB_RES::LOCAL::MEM_LOGBOOK_ENTRIES(), AsyncRequestOptions(NULL, 0, false));
    // The process will continue in onGetResult() once the entries are fetched
}

// Method to process the next log entry
void ECGIMULoggerApp::processNextLogEntry()
{
    if (mIsFetchingLogData)
    {
        // Already fetching data for a log
        return;
    }

    if (!mLogIdsToSend.isEmpty())
    {
        // Get the next log ID from the queue
        mCurrentLogId = mLogIdsToSend.dequeue();

        DEBUGLOG("Processing log ID: %d", mCurrentLogId);

        // Reset state variables
        mIsFirstDataPacket = true;
        mSendBufferLength  = 0;

        mIsFetchingLogData = true;

        // Start fetching data for the current log
        asyncGet(WB_RES::LOCAL::MEM_LOGBOOK_BYID_LOGID_DATA(), AsyncRequestOptions::ForceAsync, mCurrentLogId);
    }
    else
    {
        // No more logs to process
        DEBUGLOG("All logs have been sent.");
        sendCompletionNotification();
    }
}

// Method to send completion notification
void ECGIMULoggerApp::sendCompletionNotification()
{
    uint8_t completionMessage[2];
    completionMessage[0] = Responses::COMMAND_RESULT;
    completionMessage[1] = mLogSendReference;

    WB_RES::Characteristic dataCharValue;
    dataCharValue.bytes = wb::MakeArray<uint8_t>(completionMessage, 2);

    asyncPut(mDataCharResource, AsyncRequestOptions::Empty, dataCharValue);

    DEBUGLOG("Sent completion notification to client.");
}

// Handle chunked data sending
void ECGIMULoggerApp::handleChunkedDataSending(const uint8_t* data, size_t length, uint8_t reference)
{
    DEBUGLOG("handleChunkedDataSending(), length: %zu", length);

    size_t readIdx = 0;

    // Skip header if this is the very first data packet of the current log
    if (mIsFirstDataPacket)
    {
        mIsFirstDataPacket = false;
        readIdx = 8; // Adjust this based on your actual header size
    }

    while (readIdx < length)
    {
        int bytesLeftInSrc = length - readIdx;
        constexpr size_t MAX_SBEM_HEADER_LENGTH = 6; // (2 + 4 bytes)


        // Ensure the buffer has at least the SBEM header's worth of data
        if (mSendBufferLength < MAX_SBEM_HEADER_LENGTH)
        {
            int copyCount = MAX_SBEM_HEADER_LENGTH - mSendBufferLength;
            if (copyCount > bytesLeftInSrc)
                copyCount = bytesLeftInSrc;

            memcpy(&mSendBuffer[mSendBufferLength], &(data[readIdx]), copyCount);
            readIdx += copyCount;
            mSendBufferLength += copyCount;
            bytesLeftInSrc -= copyCount;
        }

        if (mSendBufferLength >= MAX_SBEM_HEADER_LENGTH)
        {
            uint32_t chunkId = 0, payloadLen = 0;
            uint32_t headerBytes = sbem::readChunkHeader(mSendBuffer, chunkId, payloadLen);
            DEBUGLOG("sbemChunk: id: %d, headerBytes: %d, payloadLen: %d", chunkId, headerBytes, payloadLen);

            const size_t sbemChunkSize = headerBytes + payloadLen;
            const int bytesNeededToFillSbemChunkInBuffer = sbemChunkSize - mSendBufferLength;
            const size_t bytesToCopy = WB_MIN(bytesNeededToFillSbemChunkInBuffer, bytesLeftInSrc);
            memcpy(&mSendBuffer[mSendBufferLength], &(data[readIdx]), bytesToCopy);
            readIdx += bytesToCopy;
            mSendBufferLength += bytesToCopy;
            bytesLeftInSrc -= bytesToCopy;

            if (sbemChunkSize <= mSendBufferLength)
            {
                // Prepare a temporary buffer to hold the response
                size_t totalPacketSize = payloadLen + 2; // 2 bytes for response type and reference
                uint8_t tempBuffer[20];                  // Adjust size based on MAX_BLE_PACKET_SIZE

                tempBuffer[0] = Responses::DATA;
                tempBuffer[1] = reference;
                memcpy(&tempBuffer[2], &mSendBuffer[headerBytes], payloadLen);

                // Ensure we do not exceed the maximum BLE packet size
                size_t packetSize = (totalPacketSize > MAX_BLE_PACKET_SIZE) ? MAX_BLE_PACKET_SIZE : totalPacketSize;

                WB_RES::Characteristic dataCharValue;
                dataCharValue.bytes = wb::MakeArray<uint8_t>(tempBuffer, packetSize);
                asyncPut(mDataCharResource, AsyncRequestOptions::Empty, dataCharValue);

                // Handle the remaining bytes by copying them manually, without using memmove
                size_t remainingBytes = mSendBufferLength - (headerBytes + payloadLen);
                if (remainingBytes > 0)
                {
                    // Shift remaining bytes to the start of the buffer
                    for (size_t src = headerBytes + payloadLen, dst = 0; src < mSendBufferLength; ++src, ++dst)
                    {
                        mSendBuffer[dst] = mSendBuffer[src];
                    }
                }
                mSendBufferLength = remainingBytes;
            }
        }
    }
}


void ECGIMULoggerApp::onGetResult(wb::RequestId requestId, wb::ResourceId resourceId, wb::Result resultCode, const wb::Value& result)
{
    if (resultCode != wb::HTTP_CODE_OK && resultCode != wb::HTTP_CODE_CONTINUE)
    {
        DEBUGLOG("Error fetching logs: %d", resultCode);
        mIsFetchingLogData = false;
        processNextLogEntry();
        return;
    }

    switch (resourceId.localResourceId)
    {
    case WB_RES::LOCAL::MEM_LOGBOOK_ENTRIES::LID: {
        WB_RES::LogEntries logEntries = result.convertTo<WB_RES::LogEntries>();

        // Enqueue all log IDs
        for (size_t i = 0; i < logEntries.elements.size(); ++i)
        {
            mLogIdsToSend.enqueue(logEntries.elements[i].id);
            DEBUGLOG("Enqueued log ID: %d", logEntries.elements[i].id);
        }

        // Start processing the next log entry
        processNextLogEntry();
        break;
    }




    case WB_RES::LOCAL::MEM_LOGBOOK_BYID_LOGID_DATA::LID: {
        // Get a reference to ByteStream instead of copying
        const wb::ByteStream& logData = result.convertTo<const wb::ByteStream&>();

        DEBUGLOG("Fetched log data, length: %d", logData.length());

        // Access the data pointer and size of the array
        const uint8_t* logDataPtr = logData.data; // Pointer to the array
        size_t logDataLength = logData.length();  // Length of the array

        // Chunk and send the data
        handleChunkedDataSending(logDataPtr, logDataLength, mLogSendReference);

        // Check if more data needs to be fetched
        if (resultCode == wb::HTTP_CODE_CONTINUE)
        {
            // Continue fetching data for the current log
            asyncGet(WB_RES::LOCAL::MEM_LOGBOOK_BYID_LOGID_DATA(), AsyncRequestOptions::ForceAsync, mCurrentLogId);
        }
        else
        {
            // Finished with current log, proceed to the next
            DEBUGLOG("Completed sending log ID: %d", mCurrentLogId);
            mIsFetchingLogData = false;
            processNextLogEntry();
        }
        break;
    }

    default:
        DEBUGLOG("Unhandled resourceId: %d", resourceId.localResourceId);
        break;
    }
}

void ECGIMULoggerApp::handleIncomingCommand(const wb::Array<uint8_t>& commandData)
{
    if (commandData.size() < 2)
    {
        DEBUGLOG("Invalid command received.");
        return;
    }

    uint8_t cmd = commandData[0];
    uint8_t reference = commandData[1];

    switch (cmd)
    {
    case Commands::HELLO: {
        // Respond with "Hello"
        uint8_t helloMsg[] = {Responses::COMMAND_RESULT, reference, 'H', 'e', 'l', 'l', 'o'};
        WB_RES::Characteristic dataCharValue;
        dataCharValue.bytes = wb::MakeArray<uint8_t>(helloMsg, sizeof(helloMsg));
        asyncPut(mDataCharResource, AsyncRequestOptions::ForceAsync, dataCharValue);
        break;
    }

    case Commands::SUBSCRIBE: {
        DEBUGLOG("Received SUBSCRIBE command.");

        // Create subscription logic here based on command
        // You might want to add handling for incoming subscriptions related to your app's data logging
        break;
    }

    case Commands::UNSUBSCRIBE: {
        DEBUGLOG("Received UNSUBSCRIBE command.");
        // Logic for unsubscribing any active subscriptions, e.g., stop logging
        break;
    }

    case Commands::FETCH_OFFLINE_DATA: {
        DEBUGLOG("Received FETCH_OFFLINE_DATA command.");

        // Begin sending offline data to the client
        sendOfflineData(reference);
        break;
    }

    case Commands::INIT_OFFLINE: {
        DEBUGLOG("Received INIT_OFFLINE command.");
        // Logic to clean up or initialize offline storage or log entries
        asyncDelete(WB_RES::LOCAL::MEM_LOGBOOK_ENTRIES());

        // Send confirmation response
        uint8_t initOfflineResponse[] = {Responses::COMMAND_RESULT, reference, 200}; // 200 OK
        WB_RES::Characteristic dataCharValue;
        dataCharValue.bytes = wb::MakeArray<uint8_t>(initOfflineResponse, sizeof(initOfflineResponse));
        asyncPut(mDataCharResource, AsyncRequestOptions::ForceAsync, dataCharValue);
        break;
    }

    default:
        DEBUGLOG("Unknown command received.");
        break;
    }
}


// Start Logging and Blink LED
void ECGIMULoggerApp::startLogging()
{
    if (!mLeadsConnected || mIsLogging)
        return; // Don't start if leads aren't connected or already logging

    DEBUGLOG("Starting ECG and IMU logging...");

    // Configure the DataLogger to log both ECG and IMU data
    WB_RES::DataLoggerConfig logConfig;
    WB_RES::DataEntry dataEntries[2];

    dataEntries[0].path = "/Meas/ECG/200"; // ECG data logging
    dataEntries[1].path = "/Meas/IMU6";    // IMU data logging (accelerometer + gyroscope)

    logConfig.dataEntries.dataEntry = wb::MakeArray<WB_RES::DataEntry>(dataEntries, 2);

    asyncPut(WB_RES::LOCAL::MEM_DATALOGGER_CONFIG(), AsyncRequestOptions::ForceAsync, logConfig);
    asyncPut(WB_RES::LOCAL::MEM_DATALOGGER_STATE(), AsyncRequestOptions::ForceAsync, WB_RES::DataLoggerStateValues::DATALOGGER_LOGGING);

    // Trigger LED blinking to indicate logging
    WB_RES::VisualIndType blinkType = WB_RES::VisualIndTypeValues::SHORT_VISUAL_INDICATION;
    asyncPut(WB_RES::LOCAL::UI_IND_VISUAL(), AsyncRequestOptions::Empty, blinkType);

    mIsLogging = true;
}

void ECGIMULoggerApp::stopLogging()
{
    if (!mIsLogging)
        return; // Not logging, nothing to stop

    DEBUGLOG("Stopping logging...");

    asyncPut(WB_RES::LOCAL::MEM_DATALOGGER_STATE(), AsyncRequestOptions::ForceAsync, WB_RES::DataLoggerStateValues::DATALOGGER_READY);

    mIsLogging = false;
}

// Handle BLE connection and disconnection
void ECGIMULoggerApp::handleBleConnected()
{
    mBleConnected = true;
    stopLogging(); // Stop logging when Bluetooth connects
}

void ECGIMULoggerApp::handleBleDisconnected()
{
    mBleConnected = false;

    // If the leads are connected and BLE is disconnected, start logging
    if (mLeadsConnected)
    {
        startLogging();
    }
}

// Include the implementation for handling characteristic handles after GATT service setup
void ECGIMULoggerApp::onPostResult(wb::RequestId requestId, 
                                       wb::ResourceId resourceId, 
                                       wb::Result resultCode, 
                                       const wb::Value& rResultData)
{
    DEBUGLOG("ECGIMULoggerApp::onPostResult: %d", resultCode);

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

// onNotify implementation
void ECGIMULoggerApp::onNotify(wb::ResourceId resourceId, const wb::Value& value, const wb::ParameterList& rParameters)
{
    switch (resourceId.localResourceId)
    {
    // Handle BLE peer connection and disconnection
    case WB_RES::LOCAL::COMM_BLE_PEERS::LID: {
        DEBUGLOG("Handling BLE peer connection/disconnection");
        WB_RES::PeerChange peerChange = value.convertTo<WB_RES::PeerChange>();

        if (peerChange.state == WB_RES::PeerStateValues::DISCONNECTED)
        {
            // Restart logging if BLE is disconnected
            DEBUGLOG("BLE disconnected, restarting logging.");
            handleBleDisconnected();
            if (mLeadsConnected)
            {
                startLogging();
            }
        }
        else if (peerChange.state == WB_RES::PeerStateValues::CONNECTED)
        {
            // Stop logging when Bluetooth connects
            DEBUGLOG("BLE connected, stopping logging.");
            handleBleConnected();
            stopLogging(); // Stop logging on connection
            // Data will be sent upon receiving the FETCH_OFFLINE_DATA command
        }
        break;
    }

    // Handle ECG lead connection status
    case WB_RES::LOCAL::SYSTEM_STATES_STATEID::LID: {
        DEBUGLOG("Handling ECG leads connection status");

        // Use WB_RES::StateChange instead of WB_RES::State
        WB_RES::StateChange stateChange = value.convertTo<WB_RES::StateChange>();

        // Check if the state change is related to the CONNECTOR
        if (stateChange.stateId == WB_RES::StateIdValues::CONNECTOR)
        {
            DEBUGLOG("Lead state updated. newState: %d", stateChange.newState);

            // Update the mLeadsConnected flag based on the new state
            mLeadsConnected = (stateChange.newState != 0);
            DEBUGLOG("ECG leads connected: %s", mLeadsConnected ? "true" : "false");

            // Start or stop logging based on the connection state
            if (mLeadsConnected && !mBleConnected)
            {
                startLogging();
            }
            else
            {
                stopLogging();
            }
        }
        break;
    }

    default: {
        DEBUGLOG("Unhandled notification resource ID: %d", resourceId.localResourceId);
        break;
    }
    }
}
#include "movesense.h"

#include "SimpleECGLoggerApp.h"
#include "common/core/debug.h"

#include "component_led/resources.h"
#include "component_max3000x/resources.h"
#include "mem_datalogger/resources.h"
#include "mem_logbook/resources.h"
#include "meas_hr/resources.h"
#include "comm_ble/resources.h"
#include "ui_ind/resources.h"
#include "system_mode/resources.h"
#include "system_states/resources.h"


const char* const SimpleECGLoggerApp::LAUNCHABLE_NAME = "SimpleLogger";

// Time between wake-up and going to power-off mode
#define AVAILABILITY_TIME 60000

// Time between turn on AFE wake circuit to power off
// (must be LED_BLINKING_PERIOD multiple)
#define WAKE_PREPARATION_TIME 5000

// LED blinking period in adertsing mode
#define LED_BLINKING_PERIOD 5000
// Start logging blink timeout
#define LED_START_LOGGING_BLINKING_TIMEOUT 3000

SimpleECGLoggerApp::SimpleECGLoggerApp() :
    ResourceClient(WBDEBUG_NAME(__FUNCTION__), WB_EXEC_CTX_APPLICATION),
    LaunchableModule(LAUNCHABLE_NAME, WB_EXEC_CTX_APPLICATION),
    mTimer(wb::ID_INVALID_TIMER),
    mStartLoggingTimer(wb::ID_INVALID_TIMER),
    mLeadsConnected(false),
    mDataLoggerState(WB_RES::DataLoggerStateValues::DATALOGGER_INVALID),
    mCounter(0),
    mBLEConnected(false),
    mLogsInLogbook(0xFFFFFFFF),
    mIsRecording(false)  // Ihm
{
}


SimpleECGLoggerApp::~SimpleECGLoggerApp()
{
}

bool SimpleECGLoggerApp::initModule()
{
    mModuleState = WB_RES::ModuleStateValues::INITIALIZED;
    return true;
}

void SimpleECGLoggerApp::deinitModule()
{
    mModuleState = WB_RES::ModuleStateValues::UNINITIALIZED;
}

bool SimpleECGLoggerApp::startModule()
{
    mModuleState = WB_RES::ModuleStateValues::STARTED;

    // Subscribe to leads detection
    asyncSubscribe(WB_RES::LOCAL::SYSTEM_STATES_STATEID(), AsyncRequestOptions::Empty, WB_RES::StateIdValues::CONNECTOR);

    startShutdownTimer();

    // Subscribe to BLE peers list changes
    asyncSubscribe(WB_RES::LOCAL::COMM_BLE_PEERS());

    // Check Logbook status (empty or not). The actual logging will start based on lead connection
    asyncGet(WB_RES::LOCAL::MEM_LOGBOOK_ENTRIES());

    return true;
}

void SimpleECGLoggerApp::stopModule()
{
    // Stop LED timer
    stopTimer(mTimer);
    mTimer = wb::ID_INVALID_TIMER;

    asyncUnsubscribe(WB_RES::LOCAL::COMM_BLE_PEERS());

    // Unsubscribe mem full notification
    asyncUnsubscribe(WB_RES::LOCAL::MEM_LOGBOOK_ISFULL());

    // Unsubscribe lead state
    asyncUnsubscribe(WB_RES::LOCAL::SYSTEM_STATES_STATEID(), AsyncRequestOptions::Empty, WB_RES::StateIdValues::CONNECTOR);

    mModuleState = WB_RES::ModuleStateValues::STOPPED;
}

void SimpleECGLoggerApp::startECGLogging()
{
    DEBUGLOG("startECGLogging()");

    // Configure DataLogger to record ECG and start it. Ensure timestamps and prevent overwriting
    WB_RES::DataLoggerConfig ldConfig;
    WB_RES::DataEntry entry;
    entry.path = "/Meas/ECG/200";  // Path to ECG measurement resource
    entry.timestamp = wb::CURRENT_TIME();  // Ensure data is timestamped

    // Create an array of data entries
    wb::DataEntryArray<WB_RES::DataEntry, 1> dataEntries;
    dataEntries.dataEntry[0] = entry;
    ldConfig.dataEntries = dataEntries;

    ldConfig.appendMode = true;  // Ensure data is appended, not overwritten

    // Set new config
    asyncPut(WB_RES::LOCAL::MEM_DATALOGGER_CONFIG(), AsyncRequestOptions::ForceAsync, ldConfig);

    // Subscribe to mem full notification to handle logbook full events
    asyncSubscribe(WB_RES::LOCAL::MEM_LOGBOOK_ISFULL(), AsyncRequestOptions::ForceAsync);

    // Start visual indication (e.g., LED blinking) to show that logging has started
    asyncPut(WB_RES::LOCAL::UI_IND_VISUAL(), AsyncRequestOptions::ForceAsync,
            WB_RES::VisualIndTypeValues::CONTINUOUS_VISUAL_INDICATION);

    // Optionally, start a timer to stop the visual indication after some time
    mStartLoggingTimer = startTimer(LED_START_LOGGING_BLINKING_TIMEOUT, false);

    // Start Logging
    asyncPut(WB_RES::LOCAL::MEM_DATALOGGER_STATE(), AsyncRequestOptions::ForceAsync,
             WB_RES::DataLoggerStateValues::DATALOGGER_LOGGING);
}

void SimpleECGLoggerApp::stopECGLogging()
{
    DEBUGLOG("stopECGLogging()");

    // Stop the DataLogger and set its state to READY
    asyncPut(WB_RES::LOCAL::MEM_DATALOGGER_STATE(), AsyncRequestOptions::ForceAsync,
             WB_RES::DataLoggerStateValues::DATALOGGER_READY);

    // Stop visual indication
    asyncPut(WB_RES::LOCAL::UI_IND_VISUAL(), AsyncRequestOptions::Empty,
             WB_RES::VisualIndTypeValues::NO_VISUAL_INDICATIONS);

    // Unsubscribe from logbook full notifications
    asyncUnsubscribe(WB_RES::LOCAL::MEM_LOGBOOK_ISFULL());

    // Optionally, handle any post-logging tasks here
}

void SimpleECGLoggerApp::onGetResult(whiteboard::RequestId requestId,
                              whiteboard::ResourceId resourceId,
                              whiteboard::Result resultCode,
                              const whiteboard::Value& result)
{
    switch(resourceId.localResourceId)
    {
        case WB_RES::LOCAL::MEM_DATALOGGER_STATE::LID:
        {
            WB_RES::DataLoggerState dlState = result.convertTo<WB_RES::DataLoggerState>();
            mDataLoggerState = dlState;
            break;
        }
        case WB_RES::LOCAL::MEM_LOGBOOK_ENTRIES::LID:
        {
            WB_RES::LogEntries logbookEntries = result.convertTo<WB_RES::LogEntries>();
            mLogsInLogbook = logbookEntries.elements.size();
            DEBUGLOG("Logbook has %u logs.", mLogsInLogbook);
            if (mLogsInLogbook == 0)
            {
                DEBUGLOG("Logbook was empty, starting logging");
                startECGLogging();
            }
            else
            {
                DEBUGLOG("Logbook not empty, staying idle waiting for connection");
                asyncSubscribe(WB_RES::LOCAL::MEAS_HR());
            }
            break;
        }
    }
}

void SimpleECGLoggerApp::stopShutdownTimer()
{
    DEBUGLOG("Stop shutdown timer");

    if (mTimer == whiteboard::ID_INVALID_TIMER) return;

    stopTimer(mTimer);
    mTimer = whiteboard::ID_INVALID_TIMER;
}

void SimpleECGLoggerApp::onSubscribeResult(whiteboard::RequestId requestId,
                            whiteboard::ResourceId resourceId,
                            whiteboard::Result resultCode,
                            const whiteboard::Value& result)
{
    if (resourceId.localResourceId == WB_RES::LOCAL::MEAS_HR::LID)
    {
        asyncUnsubscribe(WB_RES::LOCAL::MEAS_HR());    
    }
}

void SimpleECGLoggerApp::onNotify(wb::ResourceId resourceId,
                           const wb::Value& value,
                           const wb::ParameterList& parameters)
{
    switch (resourceId.localResourceId)
    {
    case WB_RES::LOCAL::SYSTEM_STATES_STATEID::LID:
    {
        WB_RES::StateChange stateChange = value.convertTo<WB_RES::StateChange>(); 
        if (stateChange.stateId == WB_RES::StateIdValues::CONNECTOR)
        {
            DEBUGLOG("Lead state updated. newState: %d", stateChange.newState);
            mLeadsConnected = stateChange.newState;

            if (mLeadsConnected && !mIsRecording && !mBLEConnected)
            {
                // Start recording if leads are connected and BLE is not connected
                startECGLogging();
                mIsRecording = true;
            }
            else if (!mLeadsConnected && mIsRecording)
            {
                // Stop recording if leads are disconnected
                stopECGLogging();
                mIsRecording = false;
            }
        } 
        break;
    }
    case WB_RES::LOCAL::COMM_BLE_PEERS::LID:
    {
        uint8_t peerState = value.convertTo<const WB_RES::PeerChange&>().state;
        DEBUGLOG("COMM_BLE_PEERS: peerState: %d", peerState);

        if (peerState == WB_RES::PeerStateValues::CONNECTED)
        {
            if (mIsRecording)
            {
                // Stop recording if a BLE connection is established
                stopECGLogging();
                mIsRecording = false;
            }
            mBLEConnected = true;
        }
        else if (peerState == WB_RES::PeerStateValues::DISCONNECTED)
        {
            mBLEConnected = false;

            if (mLeadsConnected && !mIsRecording)
            {
                // Optionally resume recording if BLE is disconnected and leads are connected
                startECGLogging();
                mIsRecording = true;
            }
        }

        break;
    }
    }
}


void SimpleECGLoggerApp::startShutdownTimer()
{
    DEBUGLOG("Start shutdown timer");

    if (mTimer != whiteboard::ID_INVALID_TIMER) stopTimer(mTimer);

    // Start timer
    mTimer = startTimer(LED_BLINKING_PERIOD, true);

    // Reset timeout counter
    mCounter = 0;
}

void SimpleECGLoggerApp::onTimer(wb::TimerId timerId)
{
    if (timerId == mStartLoggingTimer)
    {
        // In case of start logging timer, just stop blinking and exit
        mStartLoggingTimer = wb::ID_INVALID_TIMER;
        asyncPut(WB_RES::LOCAL::UI_IND_VISUAL(), AsyncRequestOptions::Empty,
            WB_RES::VisualIndTypeValues::NO_VISUAL_INDICATIONS);
        return;
    }

    // Check leads connection and datalogger state. if either is on, reset counter
    // NOTE: Trust that this module and datalogger are in same thread so the call is synchronous
    STATIC_VERIFY(WB_EXEC_CTX_APPLICATION == WB_RES::LOCAL::MEM_DATALOGGER_STATE::EXECUTION_CONTEXT, DataLogger_must_be_application_thread);
    asyncGet(WB_RES::LOCAL::MEM_DATALOGGER_STATE());
    if (mLeadsConnected || mDataLoggerState == WB_RES::DataLoggerStateValues::DATALOGGER_LOGGING)
    {
        DEBUGLOG("leads connected [%d] or datalogger running [%d]. postponing shutdown", mLeadsConnected, mDataLoggerState);
        mCounter = 0;
        return;
    }

    // Ok, no reason to stay awake. keep incrementing and blinking
    mCounter += LED_BLINKING_PERIOD;


    if (mCounter < AVAILABILITY_TIME)
    {
        asyncPut(WB_RES::LOCAL::UI_IND_VISUAL(), AsyncRequestOptions::Empty,
                 WB_RES::VisualIndTypeValues::SHORT_VISUAL_INDICATION);
        return;
    }

    if (mCounter == AVAILABILITY_TIME)
    {
        // Prepare AFE to wake-up mode
        asyncPut(WB_RES::LOCAL::COMPONENT_MAX3000X_WAKEUP(),
                 AsyncRequestOptions::ForceAsync, (uint8_t)1);

        // Make PUT request to switch LED on
        asyncPut(WB_RES::LOCAL::COMPONENT_LED(), AsyncRequestOptions::Empty, true);

        // Make PUT request to eneter power off mode
        asyncPut(WB_RES::LOCAL::SYSTEM_MODE(), AsyncRequestOptions::ForceAsync,
                 WB_RES::SystemModeValues::FULLPOWEROFF);
    }
}

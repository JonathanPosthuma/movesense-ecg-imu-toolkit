#pragma once

#include <whiteboard/LaunchableModule.h>
#include <whiteboard/ResourceClient.h>

class winlogger FINAL : private wb::ResourceClient, public wb::LaunchableModule
{
public:
    /** Name of this class. Used in StartupProvider list. */
    static const char* const LAUNCHABLE_NAME;
    winlogger();
    ~winlogger();


private:
    /** @see whiteboard::ILaunchableModule::initModule */
    virtual bool initModule() OVERRIDE;
    /** @see whiteboard::ILaunchableModule::deinitModule */
    virtual void deinitModule() OVERRIDE;
    /** @see whiteboard::ILaunchableModule::startModule */
    virtual bool startModule() OVERRIDE;
    /** @see whiteboard::ILaunchableModule::stopModule */
    virtual void stopModule() OVERRIDE;

    /** @see whiteboard::ResourceClient::onPostResult */
    virtual void onPostResult(wb::RequestId requestId,
                              wb::ResourceId resourceId,
                              wb::Result resultCode,
                              const wb::Value& rResultData) OVERRIDE;

    /** @see whiteboard::ResourceClient::onPutResult */
    virtual void onPutResult(wb::RequestId requestId,
                              wb::ResourceId resourceId,
                              wb::Result resultCode,
                              const wb::Value& rResultData) OVERRIDE;

    /** @see whiteboard::ResourceClient::onGetResult */
    virtual void onGetResult(wb::RequestId requestId,
                             wb::ResourceId resourceId,
                             wb::Result resultCode,
                             const wb::Value& rResultData);

    /** @see whiteboard::ResourceClient::onGetResult */
    virtual void onSubscribeResult(wb::RequestId requestId,
                                   wb::ResourceId resourceId,
                                   wb::Result resultCode,
                                   const wb::Value& rResultData);

    /** @see whiteboard::ResourceClient::onNotify */
    virtual void onNotify(wb::ResourceId resourceId,
                          const wb::Value& rValue,
                          const wb::ParameterList& rParameters);
    /**
    *	Timer callback.
    *
    *	@param timerId Id of timer that triggered
    */
    virtual void onTimer(whiteboard::TimerId timerId) OVERRIDE;

private:
    void configGattSvc();
    void unsubscribeAllStreams();
    void updateDataLoggerConfig();

    void sendOfflineData(uint8_t client_reference);
    void handleSendingLogbookData(const uint8_t *pData, uint32_t length);

    /** Start and stop logging */
    void startLogging();
    void stopLogging();

    uint32_t mLogToSend;
    uint16_t mSendBufferLength;
    bool mFirstPacketSent;
    uint8_t mLogSendReference;
    uint8_t mSendBuffer[160];

    wb::ResourceId mCommandCharResource;
    wb::ResourceId mDataCharResource;
    wb::TimerId mMeasurementTimer;
    wb::TimerId mShutdownLedTimer;

    int32_t mSensorSvcHandle;
    int32_t mCommandCharHandle;
    int32_t mDataCharHandle;

    // State tracking
    bool mBleConnected;
    bool mIsLogging;
    bool mLeadsConnected;
    bool mNotificationsEnabled;
    bool mDataloggerStopRequested;

    uint8_t mLogsInMemoryCount;
    uint32_t mLogIdToFetch;
    uint32_t mLogFetchOffset;
    uint8_t mLogFetchReference;
    uint8_t mDataLoggerState;
    int mDisconnectCounter;



    // Data subscriptions

    struct DataSub {
        wb::ResourceId resourceId;
        uint8_t clientReference;
        bool subStarted;
        bool subCompleted;
        char resourcePath[32];
        void clean() {
            memset(this, 0, sizeof(DataSub));
            resourceId = wb::ID_INVALID_RESOURCE;
        }
        bool isEmpty() const {
            return resourceId == wb::ID_INVALID_RESOURCE;
        }
    };
    static constexpr size_t MAX_DATASUB_COUNT = 4;
    DataSub mDataSubs[MAX_DATASUB_COUNT];

    DataSub *getFreeDataSubSlot();

    // Buffer for outgoing data messages (MTU -3)
    uint8_t mDataMsgBuffer[158];

    DataSub* findDataSub(const wb::ResourceId resourceId);
    DataSub* findDataSub(const wb::LocalResourceId localResourceId);
    DataSub* findDataSubByRef(const uint8_t clientReference);

    void handleIncomingCommand(const wb::Array<uint8> &commandData);

        // Timer variables
    whiteboard::TimerId mStartLoggingTimer;
    whiteboard::TimerId mStateCheckTimer;
    whiteboard::TimerId mDisconnectElapsedTime;
    uint32_t mCounter; 



    // Timer durations (constants)
    static constexpr uint32_t LED_BLINKING_PERIOD = 5000; // 5 seconds
    static constexpr uint32_t LED_START_LOGGING_BLINKING_TIMEOUT = 3000; // 3 seconds
    static constexpr uint32_t LED_DISCONNECTED_TIME = 32400000; // 9 hours 

};

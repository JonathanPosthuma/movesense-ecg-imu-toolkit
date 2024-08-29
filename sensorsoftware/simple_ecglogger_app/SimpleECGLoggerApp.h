#pragma once

#include <whiteboard/LaunchableModule.h>
#include <whiteboard/ResourceClient.h>

class SimpleECGLoggerApp FINAL : private wb::ResourceClient, public wb::LaunchableModule
{

public:
    /** Name of this class. Used in StartupProvider list. */
    static const char* const LAUNCHABLE_NAME;

    SimpleECGLoggerApp();
    ~SimpleECGLoggerApp();

private:
    /** @see whiteboard::ILaunchableModule::initModule */
    virtual bool initModule() OVERRIDE;
    /** @see whiteboard::ILaunchableModule::deinitModule */
    virtual void deinitModule() OVERRIDE;
    /** @see whiteboard::ILaunchableModule::startModule */
    virtual bool startModule() OVERRIDE;
    /** @see whiteboard::ILaunchableModule::stopModule */
    virtual void stopModule() OVERRIDE;

    /** @see whiteboard::ResourceClient::onNotify */
    virtual void onNotify(wb::ResourceId resourceId,
                          const wb::Value& value,
                          const wb::ParameterList& parameters);

    /** @see whiteboard::ResourceClient::onTimer */
    virtual void onTimer(wb::TimerId timerId) OVERRIDE;

    virtual void onGetResult(whiteboard::RequestId requestId,
                             whiteboard::ResourceId resourceId,
                             whiteboard::Result resultCode,
                             const whiteboard::Value& result) OVERRIDE;

    virtual void onSubscribeResult(whiteboard::RequestId requestId,
                             whiteboard::ResourceId resourceId,
                             whiteboard::Result resultCode,
                             const whiteboard::Value& result) OVERRIDE;


    /**
    *	Prepare to shutdown and set timer
    */
    void startShutdownTimer();
    void stopShutdownTimer();
    void startECGLogging();

    wb::TimerId mTimer;
    wb::TimerId mStartLoggingTimer;

    uint32_t mCounter;
    bool mLeadsConnected;
    bool mBLEConnected;
    uint8_t mDataLoggerState;
    size_t mLogsInLogbook;

    bool mIsRecording;  // New variable to track if ECG data is being recorded

};


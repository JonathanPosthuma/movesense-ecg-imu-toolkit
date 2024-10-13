#pragma once

#include "SimpleQueue.h" // Include SimpleQueue if used
#include <whiteboard/LaunchableModule.h>
#include <whiteboard/ResourceClient.h>

class ECGIMULoggerApp FINAL : public wb::ResourceClient, public wb::LaunchableModule
{
public:
    /** Name of this class. Used in StartupProvider list. */
    static const char* const LAUNCHABLE_NAME;

    ECGIMULoggerApp();
    ~ECGIMULoggerApp();

private:
    /** Initialize, deinitialize, start, stop the module */
    bool initModule() override;
    void deinitModule() override;
    bool startModule() override;
    void stopModule() override;

    /** Handle BLE and ECG Lead status */
    void onNotify(wb::ResourceId resourceId,
                  const wb::Value& value,
                  const wb::ParameterList& rParameters) override;

    /** Handle results from async GET operations */
    void onGetResult(wb::RequestId requestId,
                     wb::ResourceId resourceId,
                     wb::Result resultCode,
                     const wb::Value& result) override;

    /** Handle results from async POST operations */
    void onPostResult(wb::RequestId requestId,
                      wb::ResourceId resourceId,
                      wb::Result resultCode,
                      const wb::Value& rResultData) override;

    /** Setup custom GATT service */
    void setupCustomGattService();

    /** Handles BLE connection events */
    void handleBleConnected();
    void handleBleDisconnected();

    /** Start and stop logging */
    void startLogging();
    void stopLogging();

    /** Send logs to GATT client */
    void sendDataToGattClient(); // If you use this method; otherwise, you can remove it

    /** Send offline data */
    void sendOfflineData(uint8_t reference);

    /** Process the next log entry */
    void processNextLogEntry();

    /** Send completion notification */
    void sendCompletionNotification();

    /** Handle chunked data sending */
    void handleChunkedDataSending(const uint8_t* data, size_t length, uint8_t reference);

    /** Handle sending large offline data in chunks */
    void handleSendingOfflineData(const uint8_t* data, size_t length); // If still used

    /** Handle incoming GATT commands */
    void handleIncomingCommand(const wb::Array<uint8_t>& commandData); // Add this declaration

    // State tracking
    bool mBleConnected;
    bool mIsLogging;
    bool mLeadsConnected;

    // Offline data tracking
    uint32_t mLogToSend; // If still used; otherwise, you can remove it
    size_t mSendBufferLength;
    bool mFirstPacketSent;
    uint8_t mLogSendReference;
    uint8_t mSendBuffer[256]; // Adjust size as needed

    // Added missing member variables
    uint32_t mCurrentLogId;
    bool mIsFetchingLogData;
    bool mIsFirstDataPacket;
    int32_t mSensorSvcHandle;

    SimpleQueue mLogIdsToSend; // Queue for log IDs

    // GATT resources
    wb::ResourceId mCommandCharResource;
    wb::ResourceId mDataCharResource;
    uint16_t mSvcHandle; // Service handle
};

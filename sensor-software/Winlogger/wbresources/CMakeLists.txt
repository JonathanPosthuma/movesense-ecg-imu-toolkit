cmake_minimum_required(VERSION 3.4)
enable_language(C CXX ASM)
project(DefaulfFirmware)


# Download ECGRR module for build
include(FetchContent)

set(GSP_SRC_DIR "${DOWNLOADS_DIR}/gsp_module-src")
FetchContent_Declare(
    gsp_module
    URL "https://movesensestorage.blob.core.windows.net/public-artifacts/Modules/release/2.3.0/gatt_sensordata_protocol_b476.zip"
    URL_HASH MD5=2d273abf8ac75cc4fb33e1bc4704a835
    SOURCE_DIR "${GSP_SRC_DIR}" # use _cache dir to enable CircleCI caching
    DOWNLOAD_DIR "${DOWNLOADS_DIR}/gsp_module-dwnl" # use _cache dir to enable CircleCI caching
    STAMP_DIR "${DOWNLOADS_DIR}/gsp_module-stamp" # use _cache dir to enable CircleCI caching
)
FetchContent_MakeAvailable(gsp_module)

# Make sure firmware can access hrFilter class
include_directories("${CMAKE_CURRENT_LIST_DIR}/../../../nea/common/algorithms/hrFilter")

if(NOT DEFINED MOVESENSE_CORE_LIBRARY)
    # Give error that user must provide  path to movescount-core library
    message(FATAL_ERROR "Path to movesense-core library not set. Add -DMOVESENSE_CORE_LIBRARY=<path_to_core_lib>  to cmake command line")
endif()

if(NOT IS_ABSOLUTE ${MOVESENSE_CORE_LIBRARY})
    set(MOVESENSE_CORE_LIBRARY ${CMAKE_BINARY_DIR}/${MOVESENSE_CORE_LIBRARY})
endif()

# Create a list with all the modules
if(NOT DEFINED MOVESENSE_MODULES)
    list(APPEND MOVESENSE_MODULES ${ECGRR_SRC_DIR} ${GSP_SRC_DIR})
endif()

# Leave /Meas/HR as original so that it gives "straight from Maxim chip" results
add_definitions(-DECGRR_DISABLE_MEASHR_OVERRIDE=1)

include(${MOVESENSE_CORE_LIBRARY}/MovesenseFromStaticLib.cmake REQUIRED)


#-----------------------------------------------------------------
# Bundled Libs
#-----------------------------------------------------------------

# Check if the libs submodule exists and add the directory
# or error out the build
if(EXISTS "${CMAKE_SOURCE_DIR}/libs/CMakeLists.txt")
	message(STATUS "Using bundled libraries located at ${CMAKE_SOURCE_DIR}/libs")
	if(BUNDLED_CURL AND BUNDLED_ZLIB AND (BUILD_CLIENT OR BUILD_SERVER))
		# Resolve zlib before curl's OpenSSL checks can retain a failed system
		# lookup, and use the same archive and headers as the engine.
		list(APPEND CURL_CMAKE_ARGS
			"-DZLIB_LIBRARY=$<TARGET_FILE:bundled_zlib>"
			"-DZLIB_INCLUDE_DIR=${CMAKE_SOURCE_DIR}/libs/zlib"
		)
	endif()
    include(libs/CMakeLists.txt)
	if(TARGET bundled_curl AND TARGET bundled_zlib)
		# curl links its configure-time probes against this archive.
		ExternalProject_Add_StepDependencies(bundled_curl configure bundled_zlib)
	endif()
else()
	message(STATUS "======================================================")
	message(STATUS "Bundled libraries were not found on your system!")
	message(STATUS "======================================================")
	message(STATUS "You need the *multilib* package to crosscompile ET:L on a 64bit system.")
	message(STATUS "Alternatively clone etlegacy repository and then run")
	message(STATUS "        'git submodule init && git submodule update'")
	message(STATUS "and enable BUNDLED_* in CMake configuration.")
	message(FATAL_ERROR "Build stopped because of missing libraries.")
endif()

[app]
title = JobBot
package.name = jobbot
package.domain = org.jobbot

source.dir = .
source.include_exts = py,png,jpg,kv,json,txt,md

version = 2.0.0

requirements = python3,kivy==2.3.0,fpdf2,pypdf,urllib3,certifi,chardet,idna,requests,androidstorage4kivy,pyjnius,android

orientation = portrait
fullscreen = 0

android.permissions = INTERNET, ACCESS_NETWORK_STATE, READ_EXTERNAL_STORAGE, WRITE_EXTERNAL_STORAGE

android.api = 33
android.minapi = 24
android.ndk = 25b
android.ndk_api = 24
android.archs = arm64-v8a, armeabi-v7a
android.accept_sdk_license = True
android.allow_backup = True
android.release_artifact = apk

android.add_jars =
android.add_src =
android.add_aars =
android.gradle_dependencies =

android.add_activities =

android.wakelock = True
android.presplash_color = #0d47a1

p4a.branch = master
p4a.source_dir =
p4a.local_recipes =

# Android-safe: use app-private storage (handled in code)
android.private_storage = True

[buildozer]
log_level = 2
warn_on_root = 1

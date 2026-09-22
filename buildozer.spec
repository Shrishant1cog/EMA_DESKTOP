[app]
title = EMA
package.name = emaassistant
package.domain = org.emailautomater

source.dir = .
source.include_exts = py,png,jpg,kv,atlas,html,css,js,ico,json,db

version = 2.0.0

# Dependencies to cross-compile into the APK
requirements = python3,kivy,uvicorn,fastapi,pydantic,google-auth-oauthlib,google-api-python-client,groq,pypdf,docx,openpyxl,icalendar,dateutil,urllib3,requests,pyjnius

# Android Permissions
android.permissions = INTERNET,ACCESS_NETWORK_STATE,WAKE_LOCK,FOREGROUND_SERVICE,POST_NOTIFICATIONS

# Architecture
android.archs = arm64-v8a, armeabi-v7a

# Minimum and Target Android SDK
android.minapi = 26
android.api = 34
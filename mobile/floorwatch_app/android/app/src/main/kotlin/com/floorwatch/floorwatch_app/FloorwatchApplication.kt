package com.floorwatch.floorwatch_app

import android.app.Application
import com.google.firebase.FirebaseApp
import com.google.firebase.FirebaseOptions

/**
 * Starts Firebase natively, from settings the app cached after login.
 *
 * The Firebase settings (incl. the API key) live in the backend's Railway
 * variables, not in git or in the build; the app fetches them after login and
 * stores them (see PushService in Dart). This runs on EVERY process start —
 * including when Android starts the app only to deliver a push while it is
 * closed and no Dart code runs. Without a default FirebaseApp existing by
 * then, FCM cannot display that notification, which is the main case push is
 * for. Before the first login there is nothing cached, so nothing happens.
 */
class FloorwatchApplication : Application() {
    override fun onCreate() {
        super.onCreate()
        // Flutter's shared_preferences plugin writes "flutter."-prefixed keys here.
        val prefs = getSharedPreferences("FlutterSharedPreferences", MODE_PRIVATE)
        val apiKey = prefs.getString("flutter.firebase_api_key", null)
        val appId = prefs.getString("flutter.firebase_app_id", null)
        val projectId = prefs.getString("flutter.firebase_project_id", null)
        val senderId = prefs.getString("flutter.firebase_sender_id", null)
        if (apiKey.isNullOrBlank() || appId.isNullOrBlank() || projectId.isNullOrBlank() || senderId.isNullOrBlank()) return
        if (FirebaseApp.getApps(this).isNotEmpty()) return
        try {
            FirebaseApp.initializeApp(
                this,
                FirebaseOptions.Builder()
                    .setApiKey(apiKey)
                    .setApplicationId(appId)
                    .setProjectId(projectId)
                    .setGcmSenderId(senderId)
                    .build(),
            )
        } catch (_: Exception) {
            // A bad cached value must never stop the app from starting.
        }
    }
}

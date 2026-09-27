import java.io.FileInputStream
import java.util.Properties

plugins {
    id("com.android.application")
    id("kotlin-android")
    // The Flutter Gradle Plugin must be applied after the Android and Kotlin Gradle plugins.
    id("dev.flutter.flutter-gradle-plugin")
}

// Release signing. The keystore and its passwords live in android/key.properties,
// which is gitignored — see the "Releasing to staff" section of the app README for
// how to create it. Never commit the keystore or that file, and keep a backup of
// the keystore somewhere safe: Google Play will not accept updates signed with a
// different key, and a lost keystore can't be recovered.
val keystorePropertiesFile = rootProject.file("key.properties")
val keystoreProperties = Properties()
if (keystorePropertiesFile.exists()) {
    FileInputStream(keystorePropertiesFile).use { keystoreProperties.load(it) }
}

android {
    namespace = "com.floorwatch.floorwatch_app"
    compileSdk = flutter.compileSdkVersion
    ndkVersion = flutter.ndkVersion

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_11
        targetCompatibility = JavaVersion.VERSION_11
    }

    kotlinOptions {
        jvmTarget = JavaVersion.VERSION_11.toString()
    }

    defaultConfig {
        applicationId = "com.floorwatch.floorwatch_app"
        minSdk = flutter.minSdkVersion
        targetSdk = flutter.targetSdkVersion
        // Both come from pubspec.yaml's `version: <name>+<build number>`.
        versionCode = flutter.versionCode
        versionName = flutter.versionName
    }

    signingConfigs {
        if (keystorePropertiesFile.exists()) {
            create("release") {
                keyAlias = keystoreProperties["keyAlias"] as String
                keyPassword = keystoreProperties["keyPassword"] as String
                storeFile = file(keystoreProperties["storeFile"] as String)
                storePassword = keystoreProperties["storePassword"] as String
            }
        }
    }

    buildTypes {
        release {
            // With a keystore: the real release key. Without one (a developer
            // just trying `flutter run --release`), fall back to the debug key so
            // the build still works — but the guard below refuses to produce a
            // Play Store bundle that way.
            signingConfig = if (keystorePropertiesFile.exists()) {
                signingConfigs.getByName("release")
            } else {
                signingConfigs.getByName("debug")
            }
        }
    }
}

// An .aab is what gets uploaded to Google Play. One signed with the debug key is
// rejected by Play at best, and at worst locks you into the wrong key. Fail loudly
// instead of building it.
gradle.taskGraph.whenReady {
    if (hasTask(":app:bundleRelease") && !keystorePropertiesFile.exists()) {
        throw GradleException(
            "Refusing to build a Play Store bundle signed with the debug key. " +
                "Create android/key.properties first (see the app README, \"Releasing to staff\")."
        )
    }
}

flutter {
    source = "../.."
}

dependencies {
    // FloorwatchApplication.kt starts Firebase itself (from settings the server
    // sends after login — see push_service.dart), so the app module needs
    // firebase-common's FirebaseApp/FirebaseOptions on its own compile classpath;
    // Flutter plugin dependencies aren't visible here. The BoM keeps this in step
    // with the versions the plugins use.
    //
    // Deliberately NOT firebase-analytics: nothing uses it, and it would switch
    // on Google Analytics data collection in a staff app for no benefit.
    implementation(platform("com.google.firebase:firebase-bom:34.19.0"))
    implementation("com.google.firebase:firebase-common")
}

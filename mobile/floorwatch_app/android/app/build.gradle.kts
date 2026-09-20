import java.util.Properties

plugins {
    id("com.android.application")
    id("kotlin-android")
    // The Flutter Gradle Plugin must be applied after the Android and Kotlin Gradle plugins.
    id("dev.flutter.flutter-gradle-plugin")
}

// Firebase (push notifications).
//
// The real google-services.json contains the project's API key, which GitHub
// (rightly) flags when committed. So only a template is in git —
// android/app/firebase-config.json, with the key left as {{FIREBASE_API_KEY}} —
// and the real google-services.json (git-ignored) is generated from it here at
// build time. The key comes from, in order:
//   1. the FIREBASE_API_KEY environment variable, or
//   2. FIREBASE_API_KEY=... in android/firebase.properties (git-ignored; not
//      local.properties — Flutter rewrites that file on every build), or
//   3. -PFIREBASE_API_KEY=... passed to Gradle.
// A google-services.json you've placed by hand is left alone. With neither a
// file nor a key, the app builds exactly as before — just without push.
run {
    val target = file("google-services.json")
    val template = file("firebase-config.json")
    val localProps = Properties().apply {
        val f = rootProject.file("firebase.properties")
        if (f.exists()) f.inputStream().use { load(it) }
    }
    val apiKey = System.getenv("FIREBASE_API_KEY")
        ?: localProps.getProperty("FIREBASE_API_KEY")
        ?: (findProperty("FIREBASE_API_KEY") as String?)
    if (!target.exists() && template.exists() && !apiKey.isNullOrBlank()) {
        target.writeText(template.readText().replace("{{FIREBASE_API_KEY}}", apiKey))
    }
}
if (file("google-services.json").exists()) {
    apply(plugin = "com.google.gms.google-services")
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
        // TODO: Specify your own unique Application ID (https://developer.android.com/studio/build/application-id.html).
        applicationId = "com.floorwatch.floorwatch_app"
        // You can update the following values to match your application needs.
        // For more information, see: https://flutter.dev/to/review-gradle-config.
        minSdk = flutter.minSdkVersion
        targetSdk = flutter.targetSdkVersion
        versionCode = flutter.versionCode
        versionName = flutter.versionName
    }

    buildTypes {
        release {
            // TODO: Add your own signing config for the release build.
            // Signing with the debug keys for now, so `flutter run --release` works.
            signingConfig = signingConfigs.getByName("debug")
        }
    }
}

flutter {
    source = "../.."
}
dependencies {

  // Import the Firebase BoM

  implementation(platform("com.google.firebase:firebase-bom:34.19.0"))


  // TODO: Add the dependencies for Firebase products you want to use

  // When using the BoM, don't specify versions in Firebase dependencies

  implementation("com.google.firebase:firebase-analytics")


  // Add the dependencies for any other desired Firebase products

  // https://firebase.google.com/docs/android/setup#available-libraries

}

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.google.gms.google.services)
}

android {
    namespace = "cn.ololee.devhelper"
    compileSdk {
        version = release(37)
    }

    defaultConfig {
        applicationId = "cn.ololee.devhelper"
        minSdk = 29
        targetSdk = 37
        versionCode = 37
        versionName = "2.13.0"
        ndk { abiFilters += "arm64-v8a" }
        externalNativeBuild { cmake { arguments += "-DANDROID_STL=c++_shared" } }

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    // Instrumentation always targets a separate app sandbox, never the user data package.
    testBuildType = "verification"
    buildTypes {
        create("verification") {
            initWith(getByName("debug"))
            applicationIdSuffix = ".verification"
            versionNameSuffix = "-verification"
            matchingFallbacks += listOf("debug")
        }
        release {
            optimization {
                enable = false
            }
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_11
        targetCompatibility = JavaVersion.VERSION_11
    }
    buildFeatures {
        compose = true
    }
    ndkVersion = "29.0.14206865"
    externalNativeBuild {
        cmake {
            path = file("src/main/cpp/CMakeLists.txt")
            version = "3.31.6"
        }
    }
    packaging { jniLibs.useLegacyPackaging = true }
}

// This isolated offline test variant has no Firebase client registration or cloud calls.
// Keep Google Services configuration unchanged for the normal debug/release apps.
tasks.matching { it.name == "processVerificationGoogleServices" }.configureEach {
    enabled = false
}

dependencies {
    implementation(project(":protocol"))
    // User-supplied SDK, kept local because no redistribution license was supplied.
    implementation(files("libs/baidu/aPaasBase-oauth.aar", "libs/baidu/aPaasFile-oauth.aar", "libs/baidu/aPaasTransmit-oauth.aar"))
    implementation("androidx.appcompat:appcompat:1.7.1")
    implementation("androidx.localbroadcastmanager:localbroadcastmanager:1.1.0")
    implementation("androidx.recyclerview:recyclerview:1.4.0")
    implementation("androidx.constraintlayout:constraintlayout:2.2.1")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("com.github.bumptech.glide:glide:4.16.0")
    implementation("com.github.topjohnwu.libsu:core:6.0.0")
    implementation("com.google.code.gson:gson:2.14.0")
    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.activity.compose)
    // Activity result APIs need a compatible Fragment version in the transitive graph.
    implementation("androidx.fragment:fragment:1.8.9")
    implementation(libs.androidx.compose.material3)
    implementation(libs.androidx.compose.ui)
    implementation(libs.androidx.compose.ui.graphics)
    implementation(libs.androidx.compose.ui.tooling.preview)
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.lifecycle.runtime.ktx)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(libs.firebase.ai)
    testImplementation(libs.junit)
    androidTestImplementation(platform(libs.androidx.compose.bom))
    androidTestImplementation(libs.androidx.compose.ui.test.junit4)
    androidTestImplementation(libs.androidx.espresso.core)
    androidTestImplementation(libs.androidx.junit)
    debugImplementation(libs.androidx.compose.ui.test.manifest)
    debugImplementation(libs.androidx.compose.ui.tooling)
}

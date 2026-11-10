package com.speakeasy.app;

import android.app.Application;
import android.os.Build;

import java.io.FileInputStream;

import cn.jiguang.api.utils.JCollectionAuth;

/**
 * Process entry point.
 *
 * <p>JPush must be initialised even when the user never opens the "enable
 * notifications" card: registration and background delivery are SDK-owned. The
 * Activity only drives permission prompts and the UI state. Without a build-time
 * JPush AppKey the SDK is never initialised and the UI reports the build as
 * unconfigured instead of silently doing nothing.</p>
 *
 * <p>JCollectionAuth.setAuth must be called before the first JPush call: JCore
 * gates registration/collection behind that consent flag, and a missing call
 * leaves getRegistrationID() empty forever. The flag stays false until the user
 * enables notifications in Settings, then later launches pass true so push
 * keeps working without reopening Settings.</p>
 *
 * <p>JPush runs {@code PushService} in a private {@code :pushcore} process, and
 * Application.onCreate runs there too. Initialising the SDK from both processes
 * sends duplicate init/register requests, so this class only bootstraps the
 * main process; JCore initialises itself in its own process.</p>
 */
public final class SpeakEasyApp extends Application {
    @Override public void onCreate() {
        super.onCreate();
        if (!isMainProcess()) return;
        LaunchIcon.apply(this);
        try {
            JCollectionAuth.setAuth(this, PushCoordinator.consented(this));
        } catch (Throwable ignored) { }
        PushCoordinator.initialize(this);
    }

    private boolean isMainProcess() {
        try {
            String process = null;
            if (Build.VERSION.SDK_INT >= 28) {
                process = getProcessName();
            } else {
                FileInputStream in = new FileInputStream("/proc/self/cmdline");
                try {
                    byte[] buffer = new byte[128];
                    int length = in.read(buffer);
                    if (length > 0) process = new String(buffer, 0, length).trim();
                } finally {
                    in.close();
                }
            }
            return process == null || process.isEmpty() || getPackageName().equals(process);
        } catch (Throwable ignored) {
            /* If the process name cannot be read, keep the old single-process behaviour. */
            return true;
        }
    }
}

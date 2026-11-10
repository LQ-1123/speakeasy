package com.speakeasy.app;

import cn.jpush.android.service.JCommonService;

/**
 * Required by JPush since 3.x: JCore refuses to start its connection/registration
 * unless the app manifest declares a service that extends {@link JCommonService}.
 * Without it the SDK logs
 * "AndroidManifest.xml missing required service:cn.jpush.android.service.JCommonService"
 * on every init and getRegistrationID() stays empty forever.
 *
 * <p>The class itself is empty; JPush discovers it by class hierarchy and runs it in
 * the private {@code :pushcore} process (declared in AndroidManifest.xml).</p>
 */
public final class SpeakEasyPushService extends JCommonService {
}

package com.speakeasy.app;

import android.content.ComponentName;
import android.content.Context;
import android.content.pm.PackageManager;
import android.content.res.Configuration;

/**
 * Switches the launcher activity-alias so the home-screen icon follows the
 * system light/dark mode (Light tile vs Dark tile from the brand sheet).
 *
 * <p>Notifications are unaffected: they use a separate monochrome status icon
 * plus the Dynamic-gradient tile as the large icon.</p>
 */
final class LaunchIcon {
    private static final String LIGHT = "com.speakeasy.app.LauncherLight";
    private static final String DARK = "com.speakeasy.app.LauncherDark";

    private LaunchIcon() { }

    static void apply(Context context) {
        try {
            boolean night = (context.getResources().getConfiguration().uiMode
                    & Configuration.UI_MODE_NIGHT_MASK) == Configuration.UI_MODE_NIGHT_YES;
            PackageManager manager = context.getPackageManager();
            ComponentName enable = new ComponentName(context, night ? DARK : LIGHT);
            ComponentName disable = new ComponentName(context, night ? LIGHT : DARK);
            if (manager.getComponentEnabledSetting(enable) != PackageManager.COMPONENT_ENABLED_STATE_ENABLED) {
                manager.setComponentEnabledSetting(enable,
                        PackageManager.COMPONENT_ENABLED_STATE_ENABLED, PackageManager.DONT_KILL_APP);
            }
            if (manager.getComponentEnabledSetting(disable) != PackageManager.COMPONENT_ENABLED_STATE_DISABLED) {
                manager.setComponentEnabledSetting(disable,
                        PackageManager.COMPONENT_ENABLED_STATE_DISABLED, PackageManager.DONT_KILL_APP);
            }
        } catch (Throwable ignored) {
            /* Older ROMs without alias support keep the default icon. */
        }
    }
}

/**
 * Pre-hydration theme bootstrap. Runs in <head> before React paints so the last
 * valid palette is visible immediately after a refresh or WebView cold start.
 * It only touches documentElement custom properties and cannot read app data.
 */
const ROLES = [
  "primary", "on-primary", "primary-container", "on-primary-container",
  "secondary", "on-secondary", "secondary-container", "on-secondary-container",
  "tertiary", "on-tertiary", "tertiary-container", "on-tertiary-container",
  "error", "on-error", "error-container", "on-error-container",
  "surface", "on-surface", "surface-variant", "on-surface-variant",
  "surface-dim", "surface-bright", "surface-container-lowest",
  "surface-container-low", "surface-container", "surface-container-high",
  "surface-container-highest", "outline", "outline-variant",
  "inverse-surface", "inverse-on-surface", "inverse-primary", "scrim",
  "shadow", "surface-tint",
];

const CAMEL: Record<string, string> = {
  primary: "primary",
  "on-primary": "onPrimary",
  "primary-container": "primaryContainer",
  "on-primary-container": "onPrimaryContainer",
  secondary: "secondary",
  "on-secondary": "onSecondary",
  "secondary-container": "secondaryContainer",
  "on-secondary-container": "onSecondaryContainer",
  tertiary: "tertiary",
  "on-tertiary": "onTertiary",
  "tertiary-container": "tertiaryContainer",
  "on-tertiary-container": "onTertiaryContainer",
  error: "error",
  "on-error": "onError",
  "error-container": "errorContainer",
  "on-error-container": "onErrorContainer",
  surface: "surface",
  "on-surface": "onSurface",
  "surface-variant": "surfaceVariant",
  "on-surface-variant": "onSurfaceVariant",
  "surface-dim": "surfaceDim",
  "surface-bright": "surfaceBright",
  "surface-container-lowest": "surfaceContainerLowest",
  "surface-container-low": "surfaceContainerLow",
  "surface-container": "surfaceContainer",
  "surface-container-high": "surfaceContainerHigh",
  "surface-container-highest": "surfaceContainerHighest",
  outline: "outline",
  "outline-variant": "outlineVariant",
  "inverse-surface": "inverseSurface",
  "inverse-on-surface": "inverseOnSurface",
  "inverse-primary": "inversePrimary",
  scrim: "scrim",
  shadow: "shadow",
  "surface-tint": "surfaceTint",
};

export const THEME_BOOTSTRAP_SCRIPT = `(function(){try{
  var key='se.theme.v2', skey='se.theme.settings.v2';
  var stored=JSON.parse(localStorage.getItem(key)||'null');
  var settings=JSON.parse(localStorage.getItem(skey)||'{}');
  var snap=stored&&stored.snapshot; var pref=settings.preference||(stored&&stored.preference)||'system';
  var media=matchMedia('(prefers-color-scheme: dark)').matches;
  var mode=pref==='dark'||(pref==='system'&&media)?'dark':'light';
  document.documentElement.dataset.theme=mode;
  document.documentElement.dataset.transparency=settings.transparency===false?'off':'on';
  document.documentElement.dataset.reducedMotion=settings.reducedMotion?'true':'false';
  if(!snap||!snap.palettes||!snap.palettes[mode])return;
  var colors=snap.palettes[mode];
  var roles=${JSON.stringify(ROLES)};
  var camel=${JSON.stringify(CAMEL)};
  for(var i=0;i<roles.length;i++){var role=roles[i];var value=colors[camel[role]];if(value)document.documentElement.style.setProperty('--md-'+role,value);}
}catch(e){}})();`;

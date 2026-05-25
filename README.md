# eyeblink-monitor

Webcam-based blink detector with Hyprland screen dimming. Tracks eyes via MediaPipe EAR algorithm, dims screen when you forget to blink.

## NixOS (Home Manager)

```nix
# flake.nix inputs
inputs.eyeblink-monitor.url = "github:dembitskyi/eyeblink-monitor";

# In your home-manager config
imports = [ inputs.eyeblink-monitor.homeManagerModules.default ];

programs.eyeblink-monitor = {
  enable = true;
  package = inputs.eyeblink-monitor.packages.${system}.default;
  settings = {
    detection = {
      ear_threshold = 0.21;
      camera_index = 1;
    };
    alert.warning_seconds = 5;
    nudge = {
      scope = "all";
      target_dim = 0.35;
      fade_ms = 800;
      escalation = [ [ 18 0.80 ] ];
    };
  };
  extraArgs = [ "--camera" "1" ];
};
```

This generates `~/.config/eyeblink-monitor/config.toml` and starts a user systemd service bound to `graphical-session.target`.

## Manual

```sh
nix run github:your-user/eyeblink-monitor
# or in dev shell:
nix develop
uv run eyeblink-monitor --camera 1 --show-preview
```

## D-Bus

Service `org.eyeblink.Monitor` at `/org/eyeblink/Monitor`:

```sh
busctl --user introspect org.eyeblink.Monitor /org/eyeblink/Monitor
dbus-monitor --session "interface='org.eyeblink.Monitor1'"
```

Signals: `Blinked()`, `NoBlinkWarning(u seconds)`, `BlinkResumed()`.

## Config

See [`config.example.toml`](config.example.toml) for all options.

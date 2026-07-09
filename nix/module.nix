{ config, lib, pkgs, ... }:

let
  cfg = config.programs.eyeblink-monitor;
  settingsFormat = pkgs.formats.toml { };
  settingsFile = settingsFormat.generate "eyeblink-monitor.toml" cfg.settings;

  busName = cfg.settings.dbus.bus_name or "org.eyeblink.Monitor";
  # Discovery manifest consumed by os-settings: identity + where to find the
  # org.os_settings.Configurable1 object. Dropping this file into the XDG data
  # dir is what makes the module appear (as a tab) in os-settings.
  moduleManifest = builtins.toJSON {
    id = "eyeblink";
    title = "Eye Blink Monitor";
    icon = "eye";
    bus_name = busName;
    object_path = "/org/os_settings/eyeblink";
  };
in
{
  options.programs.eyeblink-monitor = {
    enable = lib.mkEnableOption "eyeblink-monitor, a webcam-based blink detector with Hyprland screen dimming";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.eyeblink-monitor;
      description = "The eyeblink-monitor package to use.";
    };

    settings = lib.mkOption {
      type = settingsFormat.type;
      default = { };
      description = "Configuration written to `~/.config/eyeblink-monitor/config.toml`.";
      example = lib.literalExpression ''
        {
          detection = {
            ear_threshold = 0.21;
            camera_index = 0;
          };
          alert.warning_seconds = 5;
          nudge = {
            scope = "all";
            target_dim = 0.35;
            fade_ms = 800;
            escalation = [ [ 18 0.80 ] ];
          };
        }
      '';
    };

    extraArgs = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = "Extra command-line arguments passed to eyeblink-monitor.";
      example = [ "--camera" "1" "--show-preview" ];
    };
  };

  config = lib.mkIf cfg.enable {
    xdg.configFile."eyeblink-monitor/config.toml" = lib.mkIf (cfg.settings != { }) {
      source = settingsFile;
    };

    # Register with os-settings so it shows up as a tunable module.
    xdg.dataFile."os-settings/modules/eyeblink.json".text = moduleManifest;

    systemd.user.services.eyeblink-monitor = {
      Unit = {
        Description = "Eyeblink Monitor — blink detection with screen dimming";
        PartOf = [ "graphical-session.target" ];
        After = [ "graphical-session.target" ];
      };

      Service = {
        Type = "simple";
        ExecStart = "${lib.getExe cfg.package} ${lib.escapeShellArgs cfg.extraArgs}";
        Restart = "on-failure";
        RestartSec = 5;
      };

      Install = {
        WantedBy = [ "graphical-session.target" ];
      };
    };
  };
}

{ config, lib, pkgs, ... }:

let
  cfg = config.programs.eyeblink-monitor;
  settingsFormat = pkgs.formats.toml { };
  settingsFile = settingsFormat.generate "eyeblink-monitor.toml" cfg.settings;
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

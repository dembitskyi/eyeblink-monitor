{
  description = "eyeblink-monitor — webcam blink detector with Hyprland screen dimming";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    {
      homeManagerModules.default = import ./nix/module.nix;
      homeManagerModules.eyeblink-monitor = self.homeManagerModules.default;

      overlays.default = final: _prev: {
        eyeblink-monitor = import ./nix/package.nix { pkgs = final; };
      };

      packages.x86_64-linux = let
        pkgs = import nixpkgs { system = "x86_64-linux"; };
      in {
        default = import ./nix/package.nix { inherit pkgs; };
        eyeblink-monitor = self.packages.x86_64-linux.default;
      };
    }
    //
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };

        runtimeLibs = with pkgs; [
          stdenv.cc.cc.lib
          zlib
          libGL
          glib
          gobject-introspection
          gtk4
          gdk-pixbuf
          graphene
          pango
          cairo
          harfbuzz
          librsvg
        ];

        giTypelibs = with pkgs; [
          glib
          gobject-introspection
          gtk4
          gdk-pixbuf
          graphene
          pango
          harfbuzz
        ];
      in
      {
        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            uv
            python312
            ruff
            mypy
            pre-commit
            gobject-introspection
            pkg-config
            cairo
            gtk4
          ];

          buildInputs = runtimeLibs;

          shellHook = ''
            export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath runtimeLibs}:$LD_LIBRARY_PATH"
            export GI_TYPELIB_PATH="${pkgs.lib.makeSearchPath "lib/girepository-1.0" giTypelibs}:$GI_TYPELIB_PATH"
            export UV_PYTHON="${pkgs.python312}/bin/python3.12"
            export UV_PYTHON_DOWNLOADS=never

            # Install git hooks (ruff + ruff-format + mypy on commit, pytest on push).
            if [ -d .git ] && [ ! -f .git/hooks/pre-push ]; then
              pre-commit install --install-hooks || true
            fi
          '';
        };
      });
}

{ pkgs, ... }:
let
  mediapipe = pkgs.python3Packages.buildPythonPackage rec {
    pname = "mediapipe";
    version = "0.10.35";
    format = "wheel";
    src = pkgs.fetchurl {
      url = "https://files.pythonhosted.org/packages/32/8f/1bc57dbc9b7b03c8f875aac23380ec57e9002cc02fe6720045fb263f3966/mediapipe-0.10.35-py3-none-manylinux_2_28_x86_64.whl";
      hash = "sha256-25pXnfSM/+lXDNPpP2pdLdCJoRA7hGxgxbXeiiHDjbA=";
    };
    nativeBuildInputs = [ pkgs.autoPatchelfHook ];
    buildInputs = with pkgs; [
      stdenv.cc.cc.lib
      libGL
      libusb1
    ];
    dependencies = with pkgs.python3Packages; [
      absl-py
      certifi
      numpy
      sounddevice
      flatbuffers
      opencv-contrib-python
      matplotlib
    ];
    pythonImportsCheck = [ "mediapipe" ];
  };
in
pkgs.python3Packages.callPackage
  (
    {
      lib,
      buildPythonApplication,
      hatchling,
      opencv-contrib-python,
      numpy,
      dasbus,
      pygobject3,
      mediapipe,
    }:
    buildPythonApplication {
      pname = "eyeblink-monitor";
      version = "0.1.0";
      pyproject = true;

      src = lib.cleanSource ../.;

      build-system = [ hatchling ];

      postPatch = ''
        # Use opencv-contrib-python from nixpkgs instead of opencv-python from PyPI.
        sed -i 's/opencv-python>=4.10/opencv-contrib-python/' pyproject.toml
        # Allow numpy 2.x since mediapipe wheel is compatible.
        sed -i 's/numpy>=1.26,<2.0/numpy/' pyproject.toml
      '';

      dependencies = [
        opencv-contrib-python
        mediapipe
        numpy
        dasbus
        pygobject3
      ];

      nativeBuildInputs = [ pkgs.gobject-introspection ];
      buildInputs = with pkgs; [
        gtk4
        glib
        gdk-pixbuf
        graphene
        pango
        cairo
        harfbuzz
        librsvg
        libGL
      ];

      # No tests yet.
      doCheck = false;

      # GI typelibs for runtime.
      preFixup = ''
        makeWrapperArgs+=(
          --prefix GI_TYPELIB_PATH : "${lib.makeSearchPath "lib/girepository-1.0" (with pkgs; [
            glib
            gobject-introspection
            gtk4
            gdk-pixbuf
            graphene
            pango
            harfbuzz
          ])}"
          --prefix LD_LIBRARY_PATH : "${lib.makeLibraryPath (with pkgs; [
            stdenv.cc.cc.lib
            libGL
          ])}"
        )
      '';

      meta = {
        description = "Webcam-based blink monitor with Hyprland screen dimming";
        license = lib.licenses.mit;
        platforms = [ "x86_64-linux" ];
        mainProgram = "eyeblink-monitor";
      };
    }
  )
  { inherit mediapipe; }

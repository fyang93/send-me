{
  description = "send-me: one-shot upload links via Cloudflare Tunnel";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { nixpkgs, flake-utils, ... }:
    flake-utils.lib.eachDefaultSystem (system:
      let pkgs = nixpkgs.legacyPackages.${system};
      in {
        devShells.default = pkgs.mkShell {
          packages = with pkgs; [ cloudflared just uv qrencode ];
          # let uv use a nix-provided python instead of downloading one
          UV_PYTHON_DOWNLOADS = "never";
          UV_PYTHON = "${pkgs.python3}/bin/python3";
        };
      });
}

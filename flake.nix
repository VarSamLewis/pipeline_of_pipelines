{
  description = "Development environment for Pipeline of Pipelines";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      mkPkgs =
        system:
        import nixpkgs {
          inherit system;
          config.allowUnfree = true;
        };
    in
    {
      devShells = forAllSystems (
        system:
        let
          pkgs = mkPkgs system;
        in
        {
          default = pkgs.mkShell {
            packages = with pkgs; [
              python313
              uv
              docker
              docker-compose
              postgresql_16
              terraform
              git
              gcc
              pkg-config
              openssl
              libffi
            ];

            PYTHONPATH = "backend/src";
            UV_PROJECT_ENVIRONMENT = ".venv";

            shellHook = ''
              if [ ! -f .env ]; then
                echo "No .env found. Copy .env.example to .env before running the app."
              fi
              if ! docker info >/dev/null 2>&1; then
                echo "Docker is unavailable. Start Docker and ensure this user can access its daemon."
              fi
              echo "Run: uv sync --frozen --extra dev"
              echo "Start Postgres: nix run .#db-up -- up -d postgres"
              echo "Run the app: nix run .#serve"
            '';
          };
        }
      );

      apps = forAllSystems (
        system:
        let
          pkgs = mkPkgs system;
          mkApp = name: text: {
            type = "app";
            program = "${
              pkgs.writeShellApplication {
                inherit name;
                runtimeInputs = with pkgs; [
                  uv
                  docker
                  docker-compose
                  git
                ];
                text = text;
              }
            }/bin/${name}";
          };
          compose = ''
            if docker compose version >/dev/null 2>&1; then
              docker compose "$@"
            else
              docker-compose "$@"
            fi
          '';
        in
        {
          db-up = mkApp "pop-db-up" ''
            ${compose}
          '';
          db-down = mkApp "pop-db-down" ''
            ${compose}
          '';
          test = mkApp "pop-test" ''
            exec uv run pytest "$@"
          '';
          check = mkApp "pop-check" ''
            uv run ruff check backend/src tests
            uv run mypy backend/src
            uv run pytest
            git diff --check
          '';
          serve = mkApp "pop-serve" ''
            exec uv run python -m uvicorn app:app --app-dir backend/src --reload "$@"
          '';
        }
      );

      formatter = forAllSystems (
        system:
        let
          pkgs = mkPkgs system;
        in
        pkgs.nixfmt
      );
    };
}

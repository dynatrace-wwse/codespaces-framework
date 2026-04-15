#!/bin/bash
# Helper script for loading environment variables and functions for building and running the Docker container locally.

getRepositoryName() {
    # Sets RepositoryName from the 'current' working directory.
    # If already set (e.g. by thin Makefile in cache mode), keep it.
    if [ -z "$RepositoryName" ]; then
        RepositoryName=$(basename "$(dirname "$PWD")")
    fi
    export RepositoryName=$RepositoryName
    echo "RepositoryName is set to: $RepositoryName"
}

detectNeededVariables() {
  # Reads the repo's post-create.sh to find variablesNeeded calls and extracts the variable specs.
  # Returns a list of VAR_NAME:required pairs.
  local post_create="${_MAKEFILE_DIR}/post-create.sh"
  if [[ -f "$post_create" ]]; then
    grep -oE 'variablesNeeded [A-Z_:a-z ]+' "$post_create" | sed 's/variablesNeeded //' | head -1
  fi
}

showNeededVariables() {
  # Displays which variables are needed for this repo, separated into required and optional.
  local var_specs
  var_specs=$(detectNeededVariables)

  if [[ -z "$var_specs" ]]; then
    echo "  (This repo does not declare required variables via variablesNeeded)"
    return
  fi

  local required=()
  local optional=()

  for var_spec in $var_specs; do
    local var_name="${var_spec%%:*}"
    local is_required="${var_spec##*:}"
    if [[ "$is_required" == "true" ]]; then
      required+=("$var_name")
    else
      optional+=("$var_name")
    fi
  done

  if [[ ${#required[@]} -gt 0 ]]; then
    echo "  REQUIRED: ${required[*]}"
  fi
  if [[ ${#optional[@]} -gt 0 ]]; then
    echo "  OPTIONAL: ${optional[*]}"
  fi
}

generateEnvExample() {
  # Generates a .env.example file from the repo's variablesNeeded declaration.
  local example_file="${_MAKEFILE_DIR}/.env.example"
  local var_specs
  var_specs=$(detectNeededVariables)

  if [[ -z "$var_specs" ]]; then
    return
  fi

  if [[ -f "$example_file" ]]; then
    return  # Don't overwrite existing .env.example
  fi

  echo "# Environment variables for $(basename "$(dirname "$_MAKEFILE_DIR")")" > "$example_file"
  echo "# Copy this file to .env and fill in the values" >> "$example_file"
  echo "# Required variables are marked with (required), optional with (optional)" >> "$example_file"
  echo "" >> "$example_file"

  for var_spec in $var_specs; do
    local var_name="${var_spec%%:*}"
    local is_required="${var_spec##*:}"
    if [[ "$is_required" == "true" ]]; then
      echo "# (required)" >> "$example_file"
    else
      echo "# (optional)" >> "$example_file"
    fi
    echo "${var_name}=" >> "$example_file"
    echo "" >> "$example_file"
  done

  echo "  Generated .env.example at $example_file"
}

getDockerEnvsFromEnvFile() {
  if [[ ! -f "$ENV_FILE" ]]; then
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  ⚠  .env file not found at $ENV_FILE"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "  This repo needs the following environment variables:"
    showNeededVariables
    echo ""
    echo "  How to fix:"
    echo "  1. Copy .env.example to .env and fill in the values:"
    echo "     cp $ENV_FILE.example $ENV_FILE"
    echo ""
    echo "  2. Or create an empty .env if you don't need Dynatrace:"
    echo "     touch $ENV_FILE"
    echo ""
    echo "  Docs: https://dynatrace-wwse.github.io/codespaces-framework/instantiation-types/#2-running-in-vs-code-dev-containers-or-local-container"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    # Generate .env.example if it doesn't exist
    generateEnvExample

    exit 1
  fi

  # Check for empty required variables and warn
  local var_specs
  var_specs=$(detectNeededVariables)
  local warned=false

  while IFS='=' read -r key value; do
    # Skip comments and empty lines
    [[ "$key" =~ ^#.*$ || -z "$key" ]] && continue
    # Remove surrounding quotes from value if present
    value="${value%\"}"
    value="${value#\"}"
    DOCKER_ENVS+=" -e ${key}=${value}"
  done < "$ENV_FILE"

  # Warn about required variables that are missing from .env
  if [[ -n "$var_specs" ]]; then
    for var_spec in $var_specs; do
      local var_name="${var_spec%%:*}"
      local is_required="${var_spec##*:}"
      # Check if the variable is present in the .env file
      if ! grep -qE "^${var_name}=.+" "$ENV_FILE"; then
        if [[ "$is_required" == "true" ]]; then
          echo "  ⚠  WARNING: Required variable $var_name is missing or empty in .env"
          warned=true
        fi
      fi
    done
    if [[ "$warned" == "true" ]]; then
      echo "  Some required variables are not set. The container may not work as expected."
      echo ""
    fi
  fi

  export DOCKER_ENVS
  echo "Loaded DOCKER_ENVS: $DOCKER_ENVS"
}


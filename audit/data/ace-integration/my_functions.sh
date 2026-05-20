#!/bin/bash
# ======================================================================
#          ------- Custom Functions -------                            #
#  Space for adding custom functions so each repo can customize as.    # 
#  needed.                                                             #
# ======================================================================

ACE_BOX_USER=$USER
export ACE_BOX_USER=$ACE_BOX_USER

customFunction(){
  printInfoSection "This is a custom function that calculates 1 + 1"

  printInfo "1 + 1 = $(( 1 + 1 ))"

}

installAceCli(){

  printInfoSection "Installing ACE Cli with sudo rights ACE_BOX_USER=$ACE_BOX_USER"

  printInfoSection " 1. Navigate to the user-skel directory"
  cd $REPO_PATH/ace-box/user-skel

  printInfoSection " 2. Install Python prerequisites"
  sudo apt-get update
  sudo apt-get install -y python3-venv ansible

  printInfoSection " 3. Install Python dependencies for ace-cli (system-wide for sudo access)"
  #break system packages added to devcontainer.
  sudo python3 -m pip install -r .ace/requirements.txt --break-system-packages

  printInfoSection " 4. Install Ansible requirements"
  ansible-galaxy install -r .ace/ansible_requirements.yml

  printInfoSection " 5. Install ACE-Box Ansible collection"
  ansible-galaxy collection install ansible_collections/ace_box/ace_box

  printInfoSection " 6. Copy ace CLI system wide"
  sudo mkdir -p /usr/local/bin
  sudo cp .ace/ace /usr/local/bin/ace
  sudo chmod +x /usr/local/bin/ace

  # printInfoSection " 7. Ensure ~/.local/bin is in your PATH"
  #echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
  #source ~/.bashrc

  printInfoSection " 8. Verify installation"
  ace --version

  printInfoSection " 9. Configuring passwordless sudo for vscode user"
  echo "vscode ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/vscode

}

configureAce(){
  printInfoSection "Creating .ace directory"
  sudo mkdir -p /home/vscode/.ace
  sudo chown vscode:vscode /home/vscode/.ace
  
  sudo ACE_BOX_USER=vscode \
  ACE_INGRESS_DOMAIN=localhost.nip.io \
  ACE_INGRESS_PROTOCOL=http \
  ACE_PUBLIC_DOMAIN=localhost.nip.io \
  ace prepare --force
}

configureAceDTNoOtel(){
  printInfoSection "Creating .ace directory"
  sudo mkdir -p /home/vscode/.ace
  sudo chown vscode:vscode /home/vscode/.ace
  
  sudo ACE_BOX_USER=vscode \
  ACE_ANSIBLE_WORKDIR=$REPO_PATH/ace-box/user-skel/ansible/ \
  ACE_INGRESS_DOMAIN=localhost.nip.io \
  ACE_INGRESS_PROTOCOL=http \
  ACE_PUBLIC_DOMAIN=localhost.nip.io \
  ACE_DT_TENANT=$DT_ENVIRONMENT \
  ACE_DT_API_TOKEN=$DT_API_TOKEN \
  ACE_DASHBOARD_USER=dynatrace \
  ACE_DASHBOARD_PASSWORD=dynatrace \
  USE_CASE_VALIDATION_TESTS_ENABLED=False \
  ANSIBLE_OPENTELEMETRY_ENABLED=False \
  ace prepare --force
}

configureAceDT(){
  printInfoSection "Creating .ace directory"
  sudo mkdir -p /home/vscode/.ace
  sudo chown vscode:vscode /home/vscode/.ace
  
  sudo ACE_BOX_USER=vscode \
  ACE_ANSIBLE_WORKDIR=$REPO_PATH/ace-box/user-skel/ansible/ \
  ACE_INGRESS_DOMAIN=localhost.nip.io \
  ACE_INGRESS_PROTOCOL=http \
  ACE_PUBLIC_DOMAIN=localhost.nip.io \
  ACE_DT_TENANT=$DT_ENVIRONMENT \
  ACE_DT_API_TOKEN=$DT_API_TOKEN \
  ACE_DASHBOARD_USER=dynatrace \
  ACE_DASHBOARD_PASSWORD=dynatrace \
  USE_CASE_VALIDATION_TESTS_ENABLED=False \
  OTEL_EXPORTER_OTLP_ENDPOINT=$DT_ENVIRONMENT/api/v2/otlp \
  OTEL_EXPORTER_OTLP_HEADERS="Authorization=Api-Token%20$DT_API_TOKEN" \
  OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf \
  ANSIBLE_OPENTELEMETRY_ENABLED=True \
  ace prepare --force
}


assertAceVersion(){
  local expected_version="${1:-0.2.0}"
  
  printInfoSection "Asserting ACE CLI version..."
  
  # Check if ace command exists
  if ! command -v ace &> /dev/null; then
    printError "ACE CLI is not installed"
    return 1
  fi
  
  # Get the actual version
  local version_output=$(ace --version 2>&1)
  local actual_version=$(echo "$version_output" | grep -oP 'version \K[0-9]+\.[0-9]+\.[0-9]+')
  
  if [ -z "$actual_version" ]; then
    printError "Could not determine ACE CLI version"
    return 1
  fi
  
  printInfo "Expected version: $expected_version"
  printInfo "Actual version: $actual_version"
  
  if [ "$actual_version" = "$expected_version" ]; then
    printInfo "✅ ACE CLI version matches expected version ✓"
    return 0
  else
    printError "❌ ACE CLI version mismatch. Expected: $expected_version, Got: $actual_version"
    return 1
  fi
}
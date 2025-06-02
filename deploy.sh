#!/usr/bin/env bash
# deploy.sh - Azure deployment script for aziots6
set -e

# Configuration
TARGET_HOSTNAME="aziots6"

# Check for --local-exec flag
USE_LOCAL_EXEC=false
if [ "$1" = "--local-exec" ]; then
    USE_LOCAL_EXEC=true
    echo "Starting deployment of $TARGET_HOSTNAME with integrated local-exec provisioners..."
else
    echo "Starting deployment of $TARGET_HOSTNAME with manual deployment..."
fi

# Source Azure environment variables
source ./set-azure-env.sh

# Change to terraform directory
cd "$(dirname "$0")/terraform"

# Initialize and apply Terraform
echo "Initializing Terraform..."
terraform init

echo "Creating/updating Azure infrastructure..."
if [ "$USE_LOCAL_EXEC" = true ]; then
    terraform apply -var="enable_local-exec=true" -auto-approve
    DEPLOYMENT_METHOD="integrated provisioners"
    SERVICE_IP="Check the terraform output above for IP and service details"
else
    terraform apply -var="enable_local-exec=false" -auto-approve
    DEPLOYMENT_METHOD="manual deployment with Ansible"
    
    # Manual deployment logic
    echo "Waiting for Azure VM to initialize and get an IP address..."
    sleep 1

    # Get the IP address from terraform output
    get_ip() {
      local ip=$(terraform output -raw vm_ip 2>/dev/null || echo "")
      
      if [ -n "$ip" ] && [ "$ip" != "null" ] && [ "$ip" != "" ]; then
        echo "$ip"
        return 0
      fi
      
      echo ""
      return 1
    }

    # Try multiple times to get a valid IP
    for i in {1..10}; do
      echo "Attempt $i to get IP address..."
      IP=$(get_ip)
      
      if [ -n "$IP" ]; then
        echo "Found IP: $IP"
        break
      fi
      
      echo "No valid IP found, waiting before trying again..."
      sleep 5
    done

    # Validate IP address
    if [ -z "$IP" ]; then
      echo "Error: Could not retrieve a valid IP address for $TARGET_HOSTNAME."
      echo "You may need to check the Azure portal to see what's happening."
      exit 1
    fi

    echo "Azure VM IP address: $IP"

    # Update Ansible inventory
    cd ../ansible
    mkdir -p inventory
    cat > inventory/hosts << EOF
[iot_servers]
$TARGET_HOSTNAME ansible_host=$IP

[all:vars]
ansible_python_interpreter=/usr/bin/python3
ansible_user=nathan
ansible_ssh_private_key_file=~/.ssh/id_rsa_azure
EOF

    # Wait for SSH to become available
    echo "Waiting for SSH to become available..."
    MAX_SSH_WAIT=300 # 5 minutes
    START_TIME=$(date +%s)

    while true; do
      if ssh -i ~/.ssh/id_rsa_azure -o StrictHostKeyChecking=no -o BatchMode=yes -o ConnectTimeout=5 nathan@"$IP" echo ready 2>/dev/null; then
        echo "SSH is available!"
        break
      fi
      
      # Check if we've waited too long
      CURRENT_TIME=$(date +%s)
      ELAPSED_TIME=$((CURRENT_TIME - START_TIME))
      
      if [ $ELAPSED_TIME -gt $MAX_SSH_WAIT ]; then
        echo "Timed out waiting for SSH. You may need to check the Azure VM console."
        exit 1
      fi
      
      echo "Still waiting for SSH..."
      sleep 10
    done

    # Run Ansible playbook
    echo "Running Ansible to configure the IoT server..."
    ansible-playbook playbooks/main.yml

    # Test the IoT services
    sleep 10
    echo "Testing IoT services..."
    
    # Test TimescaleDB connectivity
    echo "Testing TimescaleDB connection..."
    if ssh -i ~/.ssh/id_rsa_azure -o StrictHostKeyChecking=no nathan@"$IP" "PGPASSWORD=iotpass psql -h localhost -U iotuser -d iotdb -c 'SELECT 1;'" 2>/dev/null; then
        echo "✅ TimescaleDB is responding"
    else
        echo "⚠️  TimescaleDB may still be starting up"
    fi
    
    # Test MQTT broker
    echo "Testing MQTT broker..."
    if nc -z -w5 "$IP" 1883 2>/dev/null; then
        echo "✅ MQTT broker port is accessible"
    else
        echo "⚠️  MQTT broker may still be starting up"
    fi
    
    # Test Grafana dashboard
    echo "Testing Grafana dashboard..."
    if nc -z -w5 "$IP" 3000 2>/dev/null; then
        echo "✅ Grafana port 3000 is accessible"
        
        # Test Grafana HTTP endpoint
        if curl -s "http://$IP:3000/api/health" | grep -q "ok" 2>/dev/null; then
            echo "✅ Grafana is responding to HTTP requests"
        else
            echo "⚠️  Grafana port is open but service may still be starting"
        fi
    else
        echo "❌ Grafana port 3000 is not accessible"
    fi

    # Check Docker containers
    echo "Checking IoT service status..."
    ssh -i ~/.ssh/id_rsa_azure -o StrictHostKeyChecking=no nathan@"$IP" "docker ps --format 'table {{.Names}}\t{{.Status}}'" || echo "Could not check container status"
    
    # Set variables for final summary
    SERVICE_IP="$IP"

    echo ""
    echo "DEPLOYMENT COMPLETE!"

    echo "✅ Azure VM created/configured with $DEPLOYMENT_METHOD"
    if [ "$USE_LOCAL_EXEC" = false ]; then
        echo "✅ IoT Infrastructure Services:"
        echo "   • TimescaleDB: postgresql://iotuser:iotpass@$SERVICE_IP:5432/iotdb"
        echo "   • MQTT Broker: mqtt://$SERVICE_IP:1883"
        echo "   • Grafana Dashboard: http://$SERVICE_IP:3000 (admin/admin)"
        echo "   • SSH Access: nathan@$SERVICE_IP"
        echo "✅ Server IP: $SERVICE_IP"
        echo ""
        echo "💡 Update your IoT devices to use: $SERVICE_IP"
    else
        echo "✅ $SERVICE_IP"
    fi
fi

cd ..
echo "📋 Checking logs..."
./taillogs.sh
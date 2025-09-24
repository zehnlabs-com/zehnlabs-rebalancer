#!/bin/bash
# Development reload script for quick service restarts
# Usage: ./reload.sh [service-name]

case $1 in
  broker|event-broker)
    echo "🔄 Reloading event-broker..."
    docker-compose stop event-broker && docker-compose up -d event-broker
    ;;
  management|management-service)
    echo "ℹ️  Management service has auto-reload enabled (no restart needed)"
    echo "   Just make your changes and they'll be detected automatically"
    ;;
  all)
    echo "🔄 Reloading event-broker..."
    docker-compose stop event-broker && docker-compose up -d event-broker
    echo "✅ Management service uses auto-reload (no restart needed)"
    ;;
  *)
    echo "Usage: ./reload.sh [service-name]"
    echo ""
    echo "Available services:"
    echo "  event-broker        - Reload event-broker"
    echo "  management-service  - Info about management service (auto-reload)"
    echo "  all                 - Reload event-broker"
    echo ""
    echo "Examples:"
    echo "  ./reload.sh event-broker"
    echo "  ./reload.sh all"
    ;;
esac
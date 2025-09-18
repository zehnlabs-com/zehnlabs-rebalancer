import logging
import sys
import json
import os
import gzip
import shutil
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from app.config import config
from app.context import get_current_event

class CompressingTimedRotatingFileHandler(TimedRotatingFileHandler):
    """TimedRotatingFileHandler that compresses rotated files"""
    
    def doRollover(self):
        """Override to add compression after rotation"""
        # Perform the standard rollover
        super().doRollover()
        
        # Compress the rotated file
        # The rotated file will have a timestamp suffix
        # Find the most recent rotated file
        dir_name, base_name = os.path.split(self.baseFilename)
        
        try:
            file_names = os.listdir(dir_name)
            
            for file_name in file_names:
                if file_name.startswith(base_name) and not file_name.endswith('.gz') and file_name != base_name:
                    full_path = os.path.join(dir_name, file_name)
                    # Compress the file
                    with open(full_path, 'rb') as f_in:
                        with gzip.open(f'{full_path}.gz', 'wb') as f_out:
                            shutil.copyfileobj(f_in, f_out)
                    # Remove the uncompressed file
                    os.remove(full_path)
        except Exception as e:
            # Log compression errors but don't fail the rollover
            print(f"Error during log compression: {e}", file=sys.stderr)

class StructuredFormatter(logging.Formatter):
    """Custom formatter for structured logging with event_id support"""
    
    def format(self, record):
        # Start with basic log structure
        log_data = {
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S %Z'),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage()
        }
        
        # Add event_id if present in extra
        if hasattr(record, 'event_id'):
            log_data['event_id'] = record.event_id
            
        # Add account_id if present in extra
        if hasattr(record, 'account_id'):
            log_data['account_id'] = record.account_id
            
        # Add any other extra fields
        for key, value in record.__dict__.items():
            if key not in ['name', 'msg', 'args', 'levelname', 'levelno', 'pathname', 
                          'filename', 'module', 'lineno', 'funcName', 'created', 
                          'msecs', 'relativeCreated', 'thread', 'threadName', 
                          'processName', 'process', 'getMessage', 'exc_info', 
                          'exc_text', 'stack_info', 'message', 'event_id', 'account_id']:
                # Convert datetime objects to ISO format strings with timezone info for JSON serialization
                if isinstance(value, datetime):
                    log_data[key] = value.strftime('%Y-%m-%d %H:%M:%S %Z') if value.tzinfo else value.strftime('%Y-%m-%d %H:%M:%S %Z')
                else:
                    log_data[key] = value
        
        if config.logging.format == 'json':
            return json.dumps(log_data)
        else:
            # Standard format with event_id if present
            base_msg = f"{log_data['timestamp']} - {log_data['logger']} - {log_data['level']} - {log_data['message']}"
            if 'event_id' in log_data:
                base_msg += f" [event_id={log_data['event_id']}]"
            if 'account_id' in log_data:
                base_msg += f" [account_id={log_data['account_id']}]"
            return base_msg

def setup_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    
    if logger.handlers:
        return logger
    
    logger.setLevel(getattr(logging, config.logging.level.upper()))
    
    # Don't add handlers to individual loggers - let them propagate to root logger
    # This prevents duplicate log entries
    
    return logger

def configure_root_logger():
    """Configure the root logger to use structured formatting for all third-party logs"""
    root_logger = logging.getLogger()
    
    # Clear existing handlers to avoid duplicates
    root_logger.handlers.clear()
    
    # Set log level
    root_logger.setLevel(getattr(logging, config.logging.level.upper()))
    
    # Create formatter
    formatter = StructuredFormatter()
    
    # Add console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    
    # Add file handler with daily rotation and compression
    log_dir = '/app/logs'
    os.makedirs(log_dir, exist_ok=True)
    
    file_handler = CompressingTimedRotatingFileHandler(
        filename=os.path.join(log_dir, 'event-processor.log'),
        when='midnight',
        interval=1,
        backupCount=365,  # Keep 365 days of logs
        encoding='utf-8'
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
    
    # Configure specific third-party library loggers
    _configure_third_party_loggers()

def _configure_third_party_loggers():
    """Configure specific third-party library loggers with appropriate levels"""
    # ib-async: Set to WARNING to reduce noise from connection details
    ib_logger = logging.getLogger('ib_async')
    ib_logger.setLevel(logging.WARNING)
    
    # Redis: Set to INFO to capture connection issues but reduce debug noise
    redis_logger = logging.getLogger('redis')
    redis_logger.setLevel(logging.INFO)
    
    # aiohttp: Set to WARNING to reduce HTTP request/response noise
    aiohttp_logger = logging.getLogger('aiohttp')
    aiohttp_logger.setLevel(logging.WARNING)
    
    # Set access log to WARNING to reduce noise
    aiohttp_access_logger = logging.getLogger('aiohttp.access')
    aiohttp_access_logger.setLevel(logging.WARNING)

def _extract_event_properties(event):
    """Extract relevant properties from an event object for logging"""
    # First try to get event from context if not provided
    if event is None:
        event = get_current_event()

    if event is None:
        return {}

    # Since EventInfo objects are strongly typed, we can directly access core properties
    properties = {
        'event_id': event.event_id,
        'account_id': event.account_id,
        'exec_command': event.exec_command,
        'status': event.status,
        'times_queued': event.times_queued,
        'received_at': event.received_at.strftime('%Y-%m-%d %H:%M:%S %Z') if isinstance(event.received_at, datetime) else event.received_at
    }

    return properties

class AppLogger:
    """Logger instance for event-based logging with automatic event context extraction"""

    def __init__(self, name: str):
        self.logger = setup_logger(name)
    
    def log_debug(self, message: str):
        """Log debug message with automatic event context from ContextVar"""
        extra = _extract_event_properties(None)
        self.logger.debug(message, extra=extra)
    
    def log_info(self, message: str):
        """Log info message with automatic event context from ContextVar"""
        extra = _extract_event_properties(None)
        self.logger.info(message, extra=extra)
    
    def log_warning(self, message: str):
        """Log warning message with automatic event context from ContextVar"""
        extra = _extract_event_properties(None)
        self.logger.warning(message, extra=extra)
    
    def log_error(self, message: str):
        """Log error message with automatic event context from ContextVar"""
        extra = _extract_event_properties(None)
        self.logger.error(message, extra=extra)
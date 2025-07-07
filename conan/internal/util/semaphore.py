import os
import sqlite3
import sys
import time
from datetime import datetime


from conan.errors import ConanException
from conan.api.output import ConanOutput
from contextlib import contextmanager


CONAN_LOCKS_DATABASE = "my_locks.db"

def initialize_if_needed(db_file):
    conn_setup = sqlite3.connect(db_file)
    cursor_setup = conn_setup.cursor()
    cursor_setup.execute("""
                         CREATE TABLE IF NOT EXISTS locks
                         (
                             resource_name
                             TEXT
                             PRIMARY
                             KEY,
                             locked_by
                             TEXT,
                             acquired_at
                             REAL
                         );
                         """)
    conn_setup.commit()
    # conn_setup.close()

    return conn_setup  # Return the connection for further use
    # Enable WAL mode for better concurrency
    # conn_wal = sqlite3.connect(db_file)
    # conn_wal.execute("PRAGMA journal_mode=WAL;")
    # conn_wal.close()

def acquire_lock(db_path, resource_name, process_id, timeout=10):
    conn = None
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            conn = initialize_if_needed(db_path)
            # conn = sqlite3.connect(db_path, timeout=1) # timeout for connection acquisition
            cursor = conn.cursor()
            # Attempt to insert. If resource_name is PRIMARY KEY, this will fail if already exists.
            cursor.execute(
                "INSERT INTO locks (resource_name, locked_by, acquired_at) VALUES (?, ?, ?)",
                (resource_name, str(process_id), time.time())
            )
            conn.commit()
            print(f"Process {process_id} acquired lock for {resource_name}")
            return conn # Return the connection to keep the transaction open for the lock
        except sqlite3.IntegrityError:
            # Lock already exists, another process holds it
            conn.rollback() # Release any partial transaction
            conn.close()
            print(f"Process {process_id} waiting for lock on {resource_name}...")
            time.sleep(1) # Wait a bit before retrying
        except sqlite3.OperationalError as e:
            # e.g., database is locked by another transaction
            if "database is locked" in str(e):
                print(f"Process {process_id} encountered database locked error, retrying...")
                if conn:
                    conn.close()
                time.sleep(0.05) # Shorter sleep for SQLite's internal lock
            else:
                raise
    print(f"Process {process_id} failed to acquire lock for {resource_name} within timeout.")
    return None

def release_lock(conn, resource_name, process_id):
    if not conn:
        print(f"Process {process_id} tried to release a non-existent connection for {resource_name}.")
        return

    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM locks WHERE resource_name = ? AND locked_by = ?", (resource_name, str(process_id)))
        conn.commit()
        print(f"Process {process_id} released lock for {resource_name}")
    except sqlite3.Error as e:
        print(f"Error releasing lock for {resource_name} by {process_id}: {e}")
    finally:
        conn.close()


@contextmanager
def interprocess_write_lock(resource_name: str, timeout: int = 30):
    conn = acquire_lock(CONAN_LOCKS_DATABASE, resource_name, process_id=os.getpid(), timeout=timeout)
    try:
        yield
    finally:
        release_lock(conn, resource_name, process_id=os.getpid())

@contextmanager
def interprocess_read_lock(resource_name, timeout=30):
    conn = None
    start_time = time.time()
    pid_str = str(os.getpid())
    acquired_access = False

    while time.time() - start_time < timeout:
        try:
            conn = initialize_if_needed(CONAN_LOCKS_DATABASE)
            # conn = sqlite3.connect(CONAN_LOCKS_DATABASE, timeout=1) # Timeout for connecting to the DB file
            cursor = conn.cursor()

            # Check if the resource is currently locked by a writer in our 'locks' table
            cursor.execute("SELECT 1 FROM locks WHERE resource_name = ?", (resource_name,))
            is_locked_by_writer = cursor.fetchone()

            if is_locked_by_writer:
                print(f"[{pid_str}] Resource '{resource_name}' is currently being written to. Waiting...")
                conn.close() # Close connection to release any shared lock on 'locks' table
                time.sleep(0.5) # Wait a bit before retrying the check
            else:
                # Resource is not logically locked by a writer, proceed
                print(f"[{pid_str}] Resource '{resource_name}' is free. Providing read access...")
                acquired_access = True
                yield conn # Yield the connection for reading the actual application data
                return # Exit the function after yield returns

        except sqlite3.OperationalError as e:
            if "database is locked" in str(e):
                ConanOutput().debug(f"[{pid_str}] Database locked internally during check or access. Retrying...")
                if conn:
                    conn.close()
                time.sleep(0.05)
            else:
                raise
        except Exception as e:
            raise e
        finally:
            # This finally block runs IF the context manager exits naturally
            # and before the yield. If 'acquired_access' is True, it means
            # we yielded a connection, and the 'with' block's exit will handle closing it.
            # If 'acquired_access' is False (e.g., still in loop waiting),
            # we need to ensure the temporary connection for checking is closed.
            if conn and not acquired_access:
                conn.close()

    raise ConanException(f"Failed to acquire lock for '{resource_name}' within timeout.")

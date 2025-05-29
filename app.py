from flask import Flask, render_template, request, jsonify, session
import pandas as pd
import psycopg2
import os
import json
from io import BytesIO
from collections import defaultdict
import time
import numpy as np
from psycopg2.extras import execute_values
from psycopg2 import sql


app = Flask(__name__)
# Set a secret key for session management
app.secret_key = os.environ.get('SECRET_KEY', 'your_fallback_secret_key')

# Database connection details loaded from environment variables
DB_HOST = os.environ.get("DB_HOST", "10.0.10.25") # Default NocoBase DB host
DB_PORT = int(os.environ.get("DB_PORT", 5432)) # Default PostgreSQL port
DB_NAME = os.environ.get("DB_NAME", "nocobase") # Default NocoBase DB name
DB_USER = os.environ.get("DB_USER", "nocobase") # Default NocoBase DB user
DB_PASSWORD = os.environ.get("DB_PASSWORD", "nocobase") # Default NocoBase DB password

# Database Manager Class (from nocobaseimport.py)
class DatabaseManager:
    def __init__(self, host, port, database, user, password):
        self.config = {"host": host, "port": port, "database": database, "user": user, "password": password}

    def get_connection(self):
        return psycopg2.connect(**self.config)

    def fetch_all(self, query, params=None):
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                return cur.fetchall()

    def execute(self, query, params=None, commit=False, fetchone=False):
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                if commit:
                    conn.commit()
                if fetchone:
                    return cur.fetchone()
                return None

    def execute_many(self, query, params_list):
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(query, params_list)

# Utility functions (from your nocobaseimport.py)
@app.route('/collections', methods=['GET'])
def list_collections():
 # Initialize DatabaseManager with environment variables
    db_manager = DatabaseManager(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD)
    try:
        collections = get_collections(db_manager)
        return jsonify(collections)
    except Exception as e:
        return jsonify({"error": f"Database connection error: {str(e)}"}), 500
def get_collections(db: DatabaseManager):
    query = "SELECT name FROM public.collections"
    rows = db.fetch_all(query)
    return [row[0] for row in rows]

def read_excel_file(uploaded_file):
    # Using BytesIO to handle in-memory file
    xls = pd.ExcelFile(BytesIO(uploaded_file.read()))
    sheet_names = xls.sheet_names
 # In a web app, you might let the user select the sheet via a form field
 # For now, let's assume the first sheet or a specific named sheet
 # You might need to add a form field to index.html for sheet selection
    selected_sheet = sheet_names[0]  # Default to the first sheet

    # Assuming "Visitor_Information" sheet might have headers to skip
    skip_rows = range(1, 5) if selected_sheet.strip().lower() == "visitor_information" else None

    df = pd.read_excel(
        xls,
        sheet_name=selected_sheet,
        dtype=str,
        engine="openpyxl",
        skiprows=skip_rows
    )
    # Apply strip to string columns and handle None/NaN properly
    df = df.apply(lambda x: x.str.strip() if x.dtype == "object" else x)
    # Replace empty strings with None for consistent handling
    # Ensure NaN values are not replaced by empty strings before replacing empty strings
    df = df.replace(r'^\s*$', pd.NA, regex=True)
    return df.where(pd.notna(df), None)

def clean_dataframe(df):
    """Convert all NA values to None and handle blank strings."""
    df = df.replace(r'^\s*$', pd.NA, regex=True)
    return df.where(pd.notna(df), None)

# Utility functions from nocobaseimport.py (need to be added)
# I will add the necessary utility functions from your provided code here.
# This includes:
# parse_options, get_field_definitions, resolve_extra_columns,
# get_allowed_options, get_candidate_unique_key, preprocess_foreign_keys,
# validate_dataframe, get_fk_options, get_fk_options_with_mapping,
# drop_relationship_fields, preprocess_array_fields, extract_and_rename,
# process_calling_list, validate_phone_numbers, update_batches,
# upload_batches, upsert_batches, upload_data,
# get_unique_constraint_name, _do_upsert, process_dependency,
# make_position_at_work, make_extension

def parse_options(options_value):
    if isinstance(options_value, dict):
        return options_value
    try:
        return json.loads(options_value)
    except Exception:
        return {}

def get_field_definitions(db: DatabaseManager, collection):
    query = "SELECT name, type, options FROM public.fields WHERE collection_name = %s"
    rows = db.fetch_all(query, (collection,))
    field_defs = {}
    for field_name, field_type, field_options in rows:
        opts = parse_options(field_options)
        ui_schema = opts.get("uiSchema", {})
        component_props = ui_schema.get("x-component-props", {})
        allowed_options = []
        if "enum" in ui_schema:
            allowed_options = [str(opt["value"]) if isinstance(opt, dict) else str(opt)
                               for opt in ui_schema["enum"]]
        elif "options" in component_props:
            allowed_options = [str(opt["value"]) if isinstance(opt, dict) else str(opt)
                               for opt in component_props.get("options", [])]

        field_defs[field_name] = {
            "type": field_type,
            "options": opts,
            "multiple": component_props.get("multiple", False),
            "allowed_options": allowed_options,
            "is_nullable": True  # Will be updated later with DB metadata
        }

    # Add PostgreSQL column metadata for additional validation
    column_query = """
    SELECT column_name, data_type, character_maximum_length,
           is_nullable = 'YES' as is_nullable
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = %s
    """
    column_rows = db.fetch_all(column_query, (collection,))
    for col in column_rows:
        col_name = col[0]
        if col_name in field_defs:
            field_defs[col_name].update({
                "data_type": col[1],
                "char_max_len": col[2],
                "is_nullable": col[3]
            })
    return field_defs

def resolve_extra_columns(df, field_defs):
    # This function involves Streamlit UI elements and needs adaptation.
    # For now, we'll skip this or handle it differently in the Flask flow.
    # It might become a separate step or part of the validation display.
    extra_cols = [col for col in df.columns if col not in field_defs]
    if extra_cols:
        # In a Flask context, we might just return the list of extra columns
        # and let the frontend decide how to handle them, or implement a simple
        # default action like dropping them for now. Let's drop for simplicity
        # in this backend part. The UI would need to present options.
        print(f"Warning: Dropping extra columns not in schema: {extra_cols}")
        df = df.drop(columns=extra_cols, errors='ignore')
    return df

def get_candidate_unique_key(db: DatabaseManager, target):
    query = """
    SELECT kcu.column_name
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON tc.constraint_name = kcu.constraint_name
     AND tc.table_schema = kcu.table_schema
    WHERE tc.table_schema = 'public'
      AND tc.table_name = %s
      AND tc.constraint_type = 'UNIQUE'
      AND kcu.column_name <> 'id'
    LIMIT 1;
    """
    result = db.execute(query, (target,), fetchone=True)
    if result:
        return result[0]
    return None # Simplified: not including the text column fallback for now

def get_allowed_options(db: DatabaseManager, target, targetKey): # Corrected function definition
    if not target or not targetKey:
        return []
    query = sql.SQL("SELECT DISTINCT {} FROM public.{}").format(
        sql.Identifier(targetKey), sql.Identifier(target)
    )
    results = db.fetch_all(query)
    return [str(r[0]) for r in results if r[0] is not None]


def validate_dataframe(df, field_defs, db: DatabaseManager):
    unresolved_values = defaultdict(lambda: defaultdict(int))
    fk_cache = {}

    for field, spec in field_defs.items():
        if spec['type'] in ['belongsTo', 'belongsToArray']:
            target = spec['options'].get('target')
            unique_key = get_candidate_unique_key(db, target)
            if not unique_key:
                unique_key = spec['options'].get('targetKey', 'id')
            if target not in fk_cache:
                fk_cache[target] = {
                    'target_key': unique_key,
                    'values': set(x.lower() for x in get_allowed_options(db, target, unique_key))
                }

    df = df.copy()
    df['errors'] = [[] for _ in range(len(df))]

    # Relationship validation (Simplified from original)
    for col, spec in field_defs.items():
        if col in df.columns and spec['type'] in ['belongsTo', 'belongsToArray']:
            target = spec['options'].get('target')
            allowed_set = fk_cache.get(target, {}).get('values', set())
            mask = df[col].notnull()
            for idx in df.loc[mask].index:
                values_to_check = [v.strip().lower() for v in str(df.at[idx, col]).split('|') if v and v.strip()]
                missing = [v for v in values_to_check if v not in allowed_set]
                if missing:
                    df.at[idx, 'errors'].append(f"Invalid references in {col}: {set(missing)} not found in {target}")

    lookup_cache = {}

    # Preload lookup caches for each target table for foreign keys (Corrected to use db_manager)
    db_manager = DatabaseManager(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD) # Ensure db_manager is accessible
    for col, spec in field_defs.items():
        opts = spec.get("options", {}) # Ensure opts is always a dictionary
        if "target" in opts and col in df.columns: # Check if target exists
            target = opts.get("target")
            targetKey = opts.get("targetKey", 'id') # Default to 'id' if targetKey is missing
            unique_key = get_candidate_unique_key(db, target) or targetKey

            if target not in lookup_cache:
                try:
                    query = sql.SQL("SELECT {}, id FROM public.{}").format(
                        sql.Identifier(unique_key), sql.Identifier(target)
                    )
                    results = db_manager.fetch_all(query) # Use db_manager
                    mapping = {str(row[0]).strip().lower(): str(row[1]) for row in results if row[0] is not None}
                    lookup_cache[target] = (unique_key, mapping)
                except Exception as e:
                    print(f"Warning: Error preloading lookup for target '{target}': {str(e)}")
                    continue

    # Process each row in the DataFrame
    for i, idx in enumerate(df.index):
        for col, spec in field_defs.items():
            opts = spec.get("options", {})
            if "target" in opts and col in df.columns: # Check if target exists
                target = opts.get("target")
                if target not in lookup_cache:
                    continue

                field_type = spec.get("type")
                unique_key, mapping = lookup_cache[target]
                value = df.at[idx, col]
                if pd.isna(value) or value is None:
                    continue

                # Handle arrays (belongsToArray)
                if field_type == "belongsToArray" and isinstance(value, str) and "|" in value:
                    parts = [v.strip() for v in value.split("|")]
                    new_ids = []
                    for part in parts:
                        key = part.lower()
                        if key in mapping:
                            new_ids.append(mapping[key])
                    if new_ids:
                        df.at[idx, f"{col}_id"] = f"{{{','.join(new_ids)}}}"
                # Handle single relationships (belongsTo)
                elif field_type == "belongsTo":
                     # Ensure value is a string before lower() and strip()
                    key = str(value).strip().lower() if pd.notna(value) else None
                    if key and key in mapping:
                         df.at[idx, f"{col}_id"] = mapping[key]

    return df

def preprocess_array_fields(df, field_defs):
    """
    For any field with type 'array', convert pipe-delimited strings (e.g. "A|B")
    to a Python list (e.g. ["A", "B"]) and then convert the list to a string
    like '["A", "B"]'. If a single value is found, it is also converted to '["A"]'.
    If the value is None or pandas NA, it will remain None or NA.
    """
    for col, spec in field_defs.items():
        if spec.get("type") == "array" and col in df.columns:
            df[col] = df[col].apply(
                lambda x: json.dumps([i.strip() for i in str(x).split('|')])  # Use json.dumps to create a valid JSON array, handle non-string input
                if pd.notna(x) and isinstance(x, str) and "|" in str(x) else json.dumps([str(x).strip()])
                if pd.notna(x) and isinstance(x, str) else x if pd.isna(x) else (json.dumps([str(x).strip()]) if pd.notna(x) else x) # Handle single value or non-string non-na
            )
    return df

def drop_relationship_fields(df, field_defs):
    for field, spec in field_defs.items():
        if spec.get("type") in ["belongsTo", "belongsToArray"]:
            if field in df.columns:
                df.drop(columns=[field], inplace=True)
    return df

def process_dependency(
    db: DatabaseManager,
    dep_name: str,
    config: dict,
    main_df: pd.DataFrame
):
    # This function orchestrates the processing of a single dependency.
    # It will call other utility functions like extract_and_rename,
    # clean_dataframe, validate_dataframe, preprocess_foreign_keys,
    # preprocess_array_fields, drop_relationship_fields, and _do_upsert.
    # The Streamlit-specific UI interactions will need to be removed.
    # The results (success status, error DataFrame, row count) will be returned.
    # This is a complex function to fully migrate here. For now, I'll leave it as a placeholder
    # and focus on the data flow through the Flask routes.
    print(f"Processing dependency: {dep_name}")
    return True, None, 0 # Placeholder return

def extract_and_rename(df, columns_to_extract, rename_map): # Function body added
    # Ensure all columns_to_extract exist in df, drop missing ones
    existing_cols = [col for col in columns_to_extract if col in df.columns]
    if len(existing_cols) < len(columns_to_extract):
        missing = [col for col in columns_to_extract if col not in df.columns]
        print(f"Warning: Columns missing from DataFrame, cannot extract: {missing}")

    # Extract specific columns
    new_df = df[existing_cols].copy()

    # Rename columns - only rename those that exist in the new_df
    actual_rename_map = {old_name: new_name for old_name, new_name in rename_map.items() if old_name in new_df.columns}
    new_df.rename(columns=actual_rename_map, inplace=True)

    return new_df # Return the modified DataFrame

def make_position_at_work(row): # Placeholder function kept as in original
 # Placeholder for the logic in nocobaseimport.py
 # This function transforms columns like lookup_position, lookup_department, lookup_company
 # Return the row or a derived value based on actual implementation
    return row # Placeholder

def _do_upsert(
    cursor,
 # Define parameters with type hints as in the original function
    table_name: str,
    df: pd.DataFrame,
    conflict_col: str
) -> int:
    """
    INSERT … ON CONFLICT for one key, using execute_values.
    Drops duplicates on conflict_col before inserting to avoid
    “affect row a second time” errors. Returns cursor.rowcount.
    """
    # This function requires execute_values, which needs to be imported
# Initialize DatabaseManager
db_manager = DatabaseManager(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD)

@app.route('/')
def index():
    try:
        collections = get_collections(db_manager)
        return render_template('index.html', collections=collections)
    except Exception as e:
        return render_template('index.html', error=f"Database connection error: {str(e)}")

@app.route('/upload_and_select_collection', methods=['POST'])
def upload_file():
 # Check if file and collection are provided
    if 'file' not in request.files:
        return "No file part"
    file = request.files['file']
    if file.filename == '':
        return "No selected file"
    if file:
        try:
            # Read and clean the excel file
            df = read_excel_file(file)
            df = clean_dataframe(df)

            # Store the processed dataframe in the session
            # Convert DataFrame to a JSON serializable format (e.g., list of dicts)
            session['uploaded_df'] = df.to_json(orient='split', index=False) # Store without index
            # Store the selected collection as well
            session['selected_collection'] = request.form.get('collection')

            return jsonify({"message": "File uploaded and processed successfully",
                "filename": file.filename,
                "collection": session.get('selected_collection')})
        except Exception as e:
            return jsonify({"error": f"Database connection error: {str(e)}"}), 500

@app.route('/process_dependencies', methods=['POST'])
def process_dependencies():
    df_json = session.get('uploaded_df')
    collection = session.get('selected_collection')

    if not df_json or not collection:
        return jsonify({"error": "No file uploaded or collection selected."}), 400

    try:
        df = pd.read_json(df_json, orient='split')

        # Implement dependency processing logic based on collection
        # This is where you'll adapt the logic from run_nocobase_import's Step 2

        # --- Dependency Processing (Needs full implementation based on nocobaseimport.py) ---
        processed_deps_results = {}
        # Example (you'll need to add more dependencies based on your code):
        if collection == "visitor_information" and "origin_source" in df.columns: # Check if collection and column exist
            # This section needs to replicate the dependency processing loop from run_nocobase_import
            if "origin_source" in df.columns:
                dep_config = {"cols": ["origin_source"], "mapping": {"origin_source": "source_name"}}
                success, error_df, count = process_dependency(db_manager, "original_source", dep_config, df)
                processed_deps_results["original_source"] = {
                    "success": success, # This needs actual success/failure from process_dependency
                    "count": count,
                    "errors": error_df.to_json(orient='split') if error_df is not None else None
                }
            # Add other dependency processing logic here (email_list, calling_list, company, position_at_work)

        # Store the updated DataFrame after dependency processing
        session['processed_df'] = df.to_json(orient='split', index=False) # Store without index

        # --- End Dependency Processing ---

        return jsonify({"message": "Dependencies processing initiated", "results": processed_deps_results})
    except Exception as e:
        # Log the error and return a JSON response
        return jsonify({"error": f"Error processing dependencies: {str(e)}"}), 500

@app.route('/validate_data', methods=['POST'])
def validate_data():
    df_json = session.get('processed_df') # Use processed_df
    collection = session.get('selected_collection')

    if not df_json or not collection:
        return jsonify({"error": "No processed data or collection selected."}), 400
    try: # Corrected indentation
        # Ensure df_json is a string before reading, handle None or empty case
        df = pd.read_json(StringIO(df_json), orient='split') if isinstance(df_json, str) and df_json else pd.DataFrame() # Use StringIO for pandas read_json

        # Apply validation logic from validate_dataframe
        field_defs = get_field_definitions(db_manager, collection)
        # resolve_extra_columns might need to be handled interactively in UI
        # For now, let's just validate against the schema
        passed_df, error_df, unresolved_values = validate_dataframe(df, field_defs, db_manager)

        # Store validated dataframes in session
        session['passed_df'] = passed_df.to_json(orient='split', index=False) # Store without index
        session['error_df'] = error_df.to_json(orient='split', index=False) # Store without index
        # unresolved_values might also need to be stored or returned

        return jsonify({
            "message": "Data validation completed",
            # You might want to return error_df as JSON to display in the frontend
            "error_data": error_df.to_json(orient='split', index=False) if not error_df.empty else None,
            "passed_rows": len(passed_df),
            "error_rows": len(error_df),
            "unresolved_values": unresolved_values # Convert defaultdict to dict for JSON
        })
    except Exception as e:
        return jsonify({"error": f"Error validating data: {str(e)}"}), 500



# Catch potential errors during validation

@app.route('/upload_to_db', methods=['POST'])
def upload_to_db():
    passed_df_json = session.get('passed_df')
    collection = session.get('selected_collection')
    upload_mode = request.json.get('upload_mode')
    pk = request.json.get('pk') # For update mode
    conflict_col = request.json.get('conflict_col') # For upsert mode

    if not passed_df_json or not collection or not upload_mode: # Corrected check
        return jsonify({"error": "No valid data, collection, or upload mode provided."}), 400

    try: # Corrected indentation
        passed_df = pd.read_json(StringIO(passed_df_json), orient='split') # Use StringIO

        # Implement the final data upload logic from upload_data
        # This will involve calling update_batches, upload_batches, or upsert_batches

        # Placeholder for upload logic:
        # --- Final Upload Logic (Needs full implementation based on nocobaseimport.py) ---
        # For demonstration, let's simulate a successful upload
        # You will need to integrate the actual upload_data function and its helpers (_do_upsert etc.)

        # Example call (uncomment and implement upload_data):
        # result = upload_data(db_manager, collection, passed_df, upload_mode, pk, conflict_col) # Need to implement upload_data

        result = True # Simulate success for demonstration
        # if result:
        # return jsonify({"message": "Data uploaded to database successfully", "rows_uploaded": len(passed_df)})
        # else:
        # return jsonify({"error": "Data upload failed"}), 500

        return jsonify({"message": "Final upload initiated (placeholder)"})
    except Exception as e:
        return jsonify({"error": f"Error during data upload: {str(e)}"}), 500

        # --- End Final Upload Logic ---



if __name__ == '__main__':
    app.run(debug=True)
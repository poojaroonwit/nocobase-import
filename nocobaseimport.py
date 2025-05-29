import streamlit as st
import pandas as pd
import psycopg2
from psycopg2 import sql
import json
from io import BytesIO, StringIO
import time
import numpy as np
from datetime import datetime
from collections import defaultdict

# ---------------------------
# Database Manager
# ---------------------------
class DatabaseManager:
    def __init__(self, host="10.0.10.25", port=18088, database="nocobase", user="nocobase", password="nocobase"):
        self.config = {
            "host": host,
            "port": port,
            "database": database,
            "user": user,
            "password": password
        }

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
                conn.commit()
                return cur.rowcount

# ---------------------------
# Utility & Helper Functions
# ---------------------------
def parse_options(options_value):
    if isinstance(options_value, dict):
        return options_value
    try:
        return json.loads(options_value)
    except Exception:
        return {}

def get_collections(db: DatabaseManager):
    query = "SELECT name FROM public.collections"
    rows = db.fetch_all(query)
    return [row[0] for row in rows]

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
    """
    Check for extra columns in the uploaded DataFrame (i.e. columns that are not
    defined in field_defs) and allow the user to remove, map, or keep them.
    """
    extra_cols = [col for col in df.columns if col not in field_defs]
    if not extra_cols:
        st.info("No extra columns found in the upload file.")
        return df

    resolution_df = pd.DataFrame({
        "extra_column": extra_cols,
        "action": ["remove"] * len(extra_cols),
        "map_to": [None] * len(extra_cols)
    })

    st.markdown("###### Resolve Extra Columns")
    edited_df = st.data_editor(
        resolution_df,
        column_config={
            "action": st.column_config.SelectboxColumn(
                "Action", options=["remove", "map", "keep"], required=True
            ),
            "map_to": st.column_config.SelectboxColumn(
                "Map to field", options=list(field_defs.keys()),
                help="Choose a target field if action is 'map'."
            )
        },
        key="resolve_extra_columns",
        use_container_width=True,hide_index=True
    )

    for _, row in edited_df.iterrows():
        col_name = row["extra_column"]
        if row["action"] == "remove":
            if col_name in df.columns:
                df.drop(columns=[col_name], inplace=True)
        elif row["action"] == "map":
            target_field = row["map_to"]
            if target_field:
                if target_field in df.columns:
                    df[target_field] = df[target_field].fillna(df[col_name])
                else:
                    df.rename(columns={col_name: target_field}, inplace=True)
    return df

def get_allowed_options(db: DatabaseManager, target, targetKey):
    if not target or not targetKey:
        return []
    query = sql.SQL("SELECT DISTINCT {} FROM public.{}").format(
        sql.Identifier(targetKey), sql.Identifier(target)
    )
    results = db.fetch_all(query)
    return [str(r[0]) for r in results if r[0] is not None]

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
    query2 = """
    SELECT column_name
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = %s
      AND column_name <> 'id'
      AND data_type IN ('character varying', 'text')
    LIMIT 1;
    """
    result = db.execute(query2, (target,), fetchone=True)
    return result[0] if result else None

def preprocess_foreign_keys(df, field_defs, db: DatabaseManager, progress_bar=None, progress_text=None):
    total = len(df)
    start_time = time.time()
    lookup_cache = {}

    # Preload lookup caches for each target table for foreign keys
    for col, spec in field_defs.items():
        opts = spec.get("options", {})
        if "target" in opts and "targetKey" in opts and col in df.columns:
            target = opts.get("target")
            targetKey = opts.get("targetKey")
            unique_key = get_candidate_unique_key(db, target) or targetKey

            if target not in lookup_cache:
                try:
                    query = sql.SQL("SELECT {}, id FROM public.{}").format(
                        sql.Identifier(unique_key), sql.Identifier(target)
                    )
                    results = db.fetch_all(query)
                    mapping = {str(row[0]).strip().lower(): str(row[1]) for row in results if row[0] is not None}
                    lookup_cache[target] = (unique_key, mapping)
                except Exception as e:
                    st.error(f"Error preloading lookup for target '{target}': {str(e)}")
                    continue

    # Process each row in the DataFrame
    for i, idx in enumerate(df.index):
        for col, spec in field_defs.items():
            opts = spec.get("options", {})
            if "target" in opts and "targetKey" in opts and col in df.columns:
                target = opts.get("target")
                if target not in lookup_cache:
                    continue

                unique_key, mapping = lookup_cache[target]
                value = df.at[idx, col]
                if pd.isna(value) or value is None:
                    continue

                field_type = spec.get("type")
                if isinstance(value, str) and "|" in value:
                    parts = [v.strip() for v in value.split("|")]
                    new_ids = []
                    for part in parts:
                        key = part.lower()
                        if key in mapping:
                            new_ids.append(mapping[key])
                    if new_ids:
                        if field_type == "belongsToArray":
                            df.at[idx, f"{col}_id"] = f"{{{','.join(new_ids)}}}"
                        else:
                            df.at[idx, f"{col}_id"] = new_ids[0]
                else:
                    key = str(value).strip().lower()
                    if key in mapping:
                        if field_type == "belongsToArray":
                            df.at[idx, f"{col}_id"] = f"{{{mapping[key]}}}"
                        else:
                            df.at[idx, f"{col}_id"] = mapping[key]

        if progress_bar and (i % 10 == 0 or i == total - 1):
            progress = int(((i + 1) / total) * 100)
            progress_bar.progress(progress)
            elapsed = time.time() - start_time
            remaining = (elapsed / (i +1)) * (total - (i + 1))
            progress_text.markdown(
                f"**Preprocessing** {progress}% | Remaining: {datetime.utcfromtimestamp(remaining).strftime('%H:%M:%S')}"
            )

    return df

def clean_dataframe(df):
    """Convert all NA values to None and handle blank strings."""
    df = df.replace(r'^\s*$', pd.NA, regex=True)
    return df.where(pd.notna(df), None)

def validate_dataframe(df, field_defs, db: DatabaseManager):
    unresolved_values = defaultdict(lambda: defaultdict(int))
    fk_cache = {}

    # Pre-cache foreign key relationships
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

    # Clean and normalize data
    for col in field_defs:
        if col in df.columns:
            if df[col].dtype == 'object':
                df[col] = df[col].where(df[col].isnull(), df[col].str.strip())
            df[col] = df[col].where(pd.notna(df[col]), None)

    # Validate nullability
    for col, spec in field_defs.items():
        if col not in df.columns:
            continue
        if not spec.get('is_nullable', True):
            mask = df[col].isnull()
            error_msg = f"Required field {col} is missing"
            df.loc[mask, 'errors'] = df.loc[mask, 'errors'].apply(lambda errs: errs  [error_msg])

    # Validate length of values
    for col, spec in field_defs.items():
        if col not in df.columns:
            continue
        max_length = 255
        mask = df[col].notnull()
        for idx, value in df.loc[mask, col].items():
            if len(str(value)) > max_length:
                error_msg = f"Field {col} exceeds maximum length of {max_length}"
                df.at[idx, 'errors'].append(error_msg)

    # Validate numeric types
    numeric_types = {"double": float, "float": float, "number": float, "integer": int}
    for col, spec in field_defs.items():
        if col not in df.columns:
            continue
        conversion_func = numeric_types.get(spec['type'])
        if conversion_func is not None:
            mask = df[col].notnull()
            for idx, value in df.loc[mask, col].items():
                try:
                    conversion_func(value)
                except (ValueError, TypeError):
                    error_msg = f"Invalid type for field {col}: expected {spec['type']} but got '{value}'"
                    df.at[idx, 'errors'].append(error_msg)

    # Relationship validation
    for col, spec in field_defs.items():
        if col not in df.columns:
            continue
        if spec['type'] in ['belongsTo', 'belongsToArray']:
            target = spec['options'].get('target')
            allowed_set = fk_cache.get(target, {}).get('values', set())
            if spec['type'] == 'belongsTo':
                mask = df[col].notnull()
                cleaned = df.loc[mask, col].astype(str).str.strip()
                cleaned_lower = cleaned.str.lower()
                invalid_mask = ~cleaned_lower.isin(allowed_set)
                for idx in cleaned[invalid_mask].index:
                    v = cleaned.at[idx]
                    unresolved_values[col][v] = 1
                    error_msg = f"Invalid references in {col}: {{{v}}} not found in {target}"
                    df.at[idx, 'errors'].append(error_msg)
            else:
                mask = df[col].notnull()
                for idx in df.loc[mask].index:
                    values = [v.strip() for v in str(df.at[idx, col]).split('|')]
                    missing = [v for v in values if v.lower() not in allowed_set]
                    if missing:
                        for m in missing:
                            unresolved_values[col][m] = 1
                        error_msg = f"Invalid references in {col}: {set(missing)} not found in {target}"
                        df.at[idx, 'errors'].append(error_msg)

    passed_df = df[df['errors'].apply(len) == 0].drop(columns=['errors'])
    error_df = df[df['errors'].apply(len) > 0]
    return passed_df, error_df, unresolved_values

def get_fk_options(db: DatabaseManager, target, target_key, label_field=None):
    if not target or not target_key:
        return []
    
    if label_field:
        query = sql.SQL("SELECT DISTINCT {}, {} FROM public.{}").format(
            sql.Identifier(target_key),
            sql.Identifier(label_field),
            sql.Identifier(target)
        )
        results = db.fetch_all(query)
        return [
            {"value": str(row[0]).strip(), "label": str(row[1]).strip()}
            for row in results if row[0] is not None and row[1] is not None
        ]
    else:
        query = sql.SQL("SELECT DISTINCT {} FROM public.{}").format(
            sql.Identifier(target_key),
            sql.Identifier(target)
        )
        results = db.fetch_all(query)
        return [
            {"value": str(row[0]).strip(), "label": str(row[0]).strip()}
            for row in results if row[0] is not None
        ]

def get_fk_options_with_mapping(db: DatabaseManager, target, target_key, label_field=None):
    if (not label_field) or (label_field.lower() == "id"):
        candidate = get_candidate_unique_key(db, target)
        if candidate and candidate.lower() != "id":
            label_field = candidate

    if label_field and label_field.lower() != "id":
        query = sql.SQL("SELECT id, {} FROM public.{}").format(
            sql.Identifier(label_field),
            sql.Identifier(target)
        )
        results = db.fetch_all(query)
        mapping = {str(row[1]).strip().lower(): str(row[0]).strip() 
                   for row in results if row[0] is not None and row[1] is not None}
        options = list(mapping.keys())
        return options, mapping
    else:
        query = sql.SQL("SELECT id FROM public.{}").format(sql.Identifier(target))
        results = db.fetch_all(query)
        mapping = {str(row[0]).strip().lower(): str(row[0]).strip() 
                   for row in results if row[0] is not None}
        options = list(mapping.keys())
        return options, mapping

def drop_relationship_fields(df, field_defs):
    for field, spec in field_defs.items():
        if spec.get("type") in ["belongsTo", "belongsToArray"]:
            if field in df.columns:
                df.drop(columns=[field], inplace=True)
    return df

def read_excel_file(uploaded_file):
    start_time = time.time()
    xls = pd.ExcelFile(uploaded_file)
    sheet_names = xls.sheet_names
    selected_sheet = sheet_names[0] if len(sheet_names) == 1 else st.selectbox("Select sheet", sheet_names)
    skip_rows = range(1, 5) if selected_sheet.strip().lower() == "visitor_information" else None
    with st.spinner("Loading Excel data..."):
        df = pd.read_excel(
            xls, 
            sheet_name=selected_sheet,
            dtype=str,
            engine="openpyxl",
            skiprows=skip_rows
        )
        df = df.apply(lambda x: x.str.strip() if x.dtype == "object" else x)
        df = df.where(pd.notna(df), None)
        df = df.replace({"nan": "", np.nan: ""})
        st.write(f"Loaded {len(df)} rows in {time.time()-start_time:.1f}s.")
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
                lambda x: json.dumps([i.strip() for i in x.split('|')])  # Use json.dumps to create a valid JSON array
                if isinstance(x, str) and "|" in x else json.dumps([x.strip()])
                if isinstance(x, str) else x if pd.isna(x) else json.dumps([x.strip()])
            )
    return df


def extract_and_rename(df, columns_to_extract, rename_map):
    # Extract specific columns
    new_df = df[columns_to_extract].copy()
    
    # Rename columns
    new_df.rename(columns=rename_map, inplace=True)
    
    return new_df


# ---------------------------
# Main Application
# ---------------------------
from psycopg2 import sql
import pandas as pd

def process_calling_list(df):
    # Define all phone number columns and their mappings
    phone_columns = [
        ("mobile_no_1", "number_with_extension"),
        ("mobile_no_2", "number_with_extension"),
        ("telephone_extension_1", "number_with_extension"),
        ("telephone_extension_2", "number_with_extension")
    ]
    
    # Process all phone number columns in a loop
    phone_dfs = []
    for src_col, target_col in phone_columns:
        if src_col in df.columns:
            df_phone = extract_and_rename(
                df, 
                [src_col], 
                {src_col: target_col}
            )
            # Add source type for tracking
            df_phone['source_type'] = src_col
            phone_dfs.append(df_phone)
    
    # Combine all numbers and clean
    calling_list = pd.concat(phone_dfs, ignore_index=True)
    
    # Clean and validate numbers
    calling_list = (
        calling_list
        .dropna(subset=['number_with_extension'])
        .drop_duplicates(subset=['number_with_extension'])
        .pipe(validate_phone_numbers)
        .reset_index(drop=True)
    )
    
    return calling_list

def validate_phone_numbers(df):
    """Validate and standardize phone number format"""
    df['number_with_extension'] = (
        df['number_with_extension']
        .str.replace(r'\D', '', regex=True)  # Remove non-digits
        .str.replace(r'^0', '', regex=True)  # Remove leading zeros
        .apply(lambda x: x if len(x) >= 8 else None)  # Basic length check
    )
    return df.dropna(subset=['number_with_extension'])

def update_batches(cursor, table, df, pk, batch_size=10000):
    """Batch update records using primary key with proper parameterization"""
    try:
        # Safely quote identifiers
        table_name = sql.Identifier(table)
        pk_name = sql.Identifier(pk)
        
        # Get columns to update (exclude PK and updated_at)
        update_columns = [
            col for col in df.columns 
            if col not in [pk, 'updated_at', 'created_at']
        ]
        
        if not update_columns:
            raise ValueError("No updatable columns found in DataFrame")

        # Build parameterized SET clause
        # set_clause = sql.SQL(", ").join([
        #     sql.SQL("{} = %s").format(sql.Identifier(col))
        #     for col in update_columns
        # ]  [ sql.SQL("updated_at = CURRENT_TIMESTAMP") ] )

        set_clause = sql.SQL(", ").join([
            sql.SQL("{} = %s").format(sql.Identifier(col))
            for col in update_columns
        ]  [ sql.SQL("updated_at = CURRENT_TIMESTAMP") ] )

        set_elements = [
            sql.SQL("{} = %s").format(sql.Identifier(col))
            for col in update_columns
        ]
        set_elements.append( sql.SQL("updated_at = CURRENT_TIMESTAMP") )

        set_clause = sql.SQL(", ").join(set_elements)


        # set_clause = sql.SQL(", ").join([
        #     sql.SQL("{} = EXCLUDED.{}").format(
        #         sql.Identifier(col), 
        #         sql.Identifier(col)
        #     ) for col in update_columns
        # ]  [sql.SQL("updated_at = CURRENT_TIMESTAMP")])

        # Build full parameterized query
        query = sql.SQL("""
            UPDATE {table}
            SET {set_clause}
            WHERE {pk} = %s
        """).format(
            table=table_name,
            set_clause=set_clause,
            pk=pk_name
        )

        # Prepare data: convert to list of tuples with (update_values..., pk_value)
        data = df[update_columns  [pk]].where(pd.notnull(df), None)
        
        data = data.to_numpy().tolist()

        # Batch execution
        for i in range(0, len(data), batch_size):
            batch = data[i:i+batch_size]
            cursor.executemany(query, batch)
            
        return True
    except Exception as e:
        cursor.connection.rollback()
        raise e
def upload_batches(cursor, table, df, batch_size=10000):
    """Generic batch insert"""
    for i in range(0, len(df), batch_size):
        batch = df.iloc[i:i+batch_size]
        columns = [col for col in batch.columns if col != 'created_at']
        placeholders = ', '.join(['%s'] * len(columns))  # fix placeholder string
        query = f"""
            INSERT INTO {table} ({', '.join(columns)}, created_at)
            VALUES ({placeholders}, CURRENT_TIMESTAMP)
        """
        cursor.executemany(query, batch[columns].to_records(index=False))


def upsert_batches(cursor, table, df, conflict_col, batch_size=10000):
    """Batch upsert (insert or update on conflict) with proper parameterization"""
    # 1) Make sure the conflict column exists
    if conflict_col not in df.columns:
        raise ValueError(f"Conflict column '{conflict_col}' not found in DataFrame")

    # 2) Determine which columns to insert (everything except created_at)
    insert_columns = [col for col in df.columns if col != 'created_at']
    quoted_insert_cols = [sql.Identifier(col) for col in insert_columns]

    # 3) Build the SET clause for the UPDATE part:
    #    one "col = EXCLUDED.col" per updateable column + "updated_at = CURRENT_TIMESTAMP"
    update_cols = [col for col in insert_columns if col not in [conflict_col, 'updated_at']]
    set_elements = [
        sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(col), sql.Identifier(col))
        for col in update_cols
    ]
    set_elements.append(sql.SQL("updated_at = CURRENT_TIMESTAMP"))
    set_clause = sql.SQL(", ").join(set_elements)

    # 4) Assemble the full INSERT…ON CONFLICT query
    query = sql.SQL("""
        INSERT INTO {table} ({cols}, created_at)
        VALUES ({vals}, CURRENT_TIMESTAMP)
        ON CONFLICT ({conflict})
        DO UPDATE SET {set_clause}
    """).format(
        table=sql.Identifier(table),
        cols=sql.SQL(', ').join(quoted_insert_cols),
        vals=sql.SQL(', ').join([sql.Placeholder()] * len(insert_columns)),
        conflict=sql.Identifier(conflict_col),
        set_clause=set_clause
    )

    # 5) Prepare the data: convert NA to None, then to list of tuples
    data_df = df[insert_columns].where(pd.notnull(df[insert_columns]), None)
    data = [tuple(row) for row in data_df.to_numpy()]

    # 6) Execute in batches
    for i in range(0, len(data), batch_size):
        batch = data[i : i + batch_size]
        cursor.executemany(query, batch)

    return True

def upload_data(db, collection, df, mode="insert", pk=None, conflict_col=None):
    """Handle final data upload with different modes"""
    try:
        with db.get_connection() as conn:
            cursor = conn.cursor()
            
            if mode == "insert":
                upload_batches(cursor, collection, df)
            elif mode == "update":
                update_batches(cursor, collection, df, pk)
            elif mode == "insert on duplicate update":
                upsert_batches(cursor, collection, df, conflict_col)
            
            conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        st.error(f"Upload error: {str(e)}")
        return False
    



from typing import Tuple, Optional
import streamlit as st
import pandas as pd
import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_values

def get_unique_constraint_name(db, table: str, column: str) -> Optional[str]:
    """
    Returns the name of a UNIQUE or PRIMARY KEY constraint on `column`, if any.
    """
    query = """
    SELECT tc.constraint_name
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON tc.constraint_name = kcu.constraint_name
     AND tc.table_schema = kcu.table_schema
    WHERE tc.table_schema = 'public'
      AND tc.table_name   = %s
      AND tc.constraint_type IN ('UNIQUE')
      AND kcu.column_name = %s
    LIMIT 1;
    """
    rows = db.fetch_all(query, (table, column))
    return rows[0][0] if rows else None

def _do_upsert(
    cursor,
    table_name: str,
    df: pd.DataFrame,
    conflict_col: str
) -> int:
    """
    INSERT … ON CONFLICT for one key, using execute_values.
    Drops duplicates on conflict_col before inserting to avoid
    “affect row a second time” errors. Returns cursor.rowcount.
    """
    # 1) Deduplicate on the conflict key
    if conflict_col in df.columns:
        df = df.drop_duplicates(subset=[conflict_col], keep="last")

    cols = list(df.columns)
    col_idents  = [sql.Identifier(c) for c in cols]
    update_cols = [c for c in cols if c != conflict_col]

    if update_cols:
        set_clause = sql.SQL(", ").join(
            sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(c), sql.Identifier(c))
            for c in update_cols
        )
        conflict_clause = sql.SQL(" ON CONFLICT ({}) DO UPDATE SET {}").format(
            sql.Identifier(conflict_col),
            set_clause
        )
    else:
        conflict_clause = sql.SQL(" ON CONFLICT ({}) DO NOTHING").format(
            sql.Identifier(conflict_col)
        )

    insert_sql = sql.SQL("INSERT INTO public.{} ({}) VALUES %s{}").format(
        sql.Identifier(table_name),
        sql.SQL(", ").join(col_idents),
        conflict_clause
    )

    data = [tuple(r) for r in df.itertuples(index=False, name=None)]
    if not data:
        return 0

    execute_values(cursor, insert_sql, data)
    return cursor.rowcount
def process_dependency(
    db: DatabaseManager,
    dep_name: str,
    config: dict,
    main_df: pd.DataFrame
) -> Tuple[bool, Optional[pd.DataFrame], int]:
    """
    1) Normalize & drop raw duplicates
    2) Extract & clean
    3) Validate & enrich
    4) Drop full-row duplicates
    5) Upsert on first column as conflict key
    6) Return (success, error_df, rows_upserted)
    """
    error_df = None
    try:
        # 1) Normalize & drop duplicates
        # df = main_df.astype(str).drop_duplicates(keep="last")
        # df = main_df.drop_duplicates(keep="last")
        df = main_df
        if df.empty:
            st.warning(f"No input rows for '{dep_name}', skipping.")
            return True, None, 0
        st.info(f"🔄 before clean data ")
        # 2) Extract & clean
        dep_df = extract_and_rename(df, config["cols"], config["mapping"])
        dep_df = clean_dataframe(dep_df)
        st.info(f"🔄 clean data ")

        # 3) Validate & preprocess
        field_defs          = get_field_definitions(db, dep_name)
        passed_df, error_df, _ = validate_dataframe(dep_df, field_defs, db)
        passed_df = preprocess_foreign_keys(passed_df, field_defs, db)
        passed_df = preprocess_array_fields(passed_df, field_defs)
        passed_df = drop_relationship_fields(passed_df, field_defs)
        passed_df = passed_df.where(~passed_df.isna(), None)
        st.info(f"🔄 de process ")
        if passed_df.empty:
            st.warning(f"No valid rows for '{dep_name}', skipping upsert.")
            return True, error_df, 0

        # 4) Drop any remaining full-row duplicates
        passed_df = passed_df.drop_duplicates(keep="last")
        rows_to_upsert = len(passed_df)
        st.info(f"🔄 {dep_name}: upserting {rows_to_upsert:,} rows")

        # 5) Use the first column as the conflict key
        conflict_col = passed_df.columns[0]

        # 6) Perform upsert
        conn = db.get_connection()
        cur  = conn.cursor()
        try:
            rowcount = _do_upsert(cur, dep_name, passed_df, conflict_col)
            conn.commit()
        except Exception as e:
            conn.rollback()
            st.error(f"Upsert failed for {dep_name}: {e}")
            return False, error_df, 0
        finally:
            cur.close()
            conn.close()

        return True, None, rowcount

    except Exception as e:
        st.error(f"Error processing {dep_name}: {e}")
        return False, error_df, 0
def make_position_at_work(row):
    # Helper to split and clean or fallback to ['Unknown']
    def split_or_default(val, default):
        if pd.isna(val) or not str(val).strip():
            return [default]
        return [v.strip() for v in str(val).split('|')]

    # Split all fields
    positions = split_or_default(row.get('lookup_position'), 'Unknown position')
    departments = split_or_default(row.get('lookup_department'), '')  # dept is optional
    companies = split_or_default(row.get('lookup_company'), 'Unknown Company')

    # Normalize lengths
    max_len = max(len(positions), len(departments), len(companies))
    positions *= max_len // len(positions) + (max_len % len(positions) > 0)
    departments *= max_len // len(departments) + (max_len % len(departments) > 0)
    companies *= max_len // len(companies) + (max_len % len(companies) > 0)

    # Construct results
    results = []
    for pos, dept, comp in zip(positions, departments, companies):
        dept_part = f",{dept}" if dept else ''
        results.append(f"{pos}{dept_part} at {comp}")

    return '|'.join(results)
# Helper to combine a number + extension into a single string (or None)
def make_extension(row, num_col, ext_col):
    num = row.get(num_col)
    ext = row.get(ext_col)

    # Normalize empty/nan → None
    if pd.isna(num) or str(num) == "" or str(num) == None:
        num = None
    if pd.isna(ext) or str(ext).strip() == "" or str(ext) == None:
        ext = None

    if num and ext:
        return f"{num}-{ext}"
    elif num:
        return str(num)
    elif ext:
        return str(ext)
    else:
        return None
def run_nocobase_import():
    # st.set_page_config(page_title="NocoBase Importer", layout="wide")
    # st.title("🚀 NocoBase Data Importer")

    if 'dep_counts' not in st.session_state:
        st.session_state.dep_counts = {}
    
    # Initialize session state
    if 'current_step' not in st.session_state:
        st.session_state.current_step = 1
    if 'processed_deps' not in st.session_state:
        st.session_state.processed_deps = {}

    # Step progress header
    steps = ["Upload File", "Process Dependencies", "Validate Data", "Upload Data"]
    header_cols = st.columns(len(steps))
    for i, col in enumerate(header_cols):
        with col:
            step_num = i + 1
            circle = "⬤" if step_num <= st.session_state.current_step else "○"
            col.markdown(f"<h4 style='text-align: center'>{circle}<br>{steps[i]}</h4>", 
                        unsafe_allow_html=True)

    db = DatabaseManager()
    
    # Step 1: File Upload & Configuration
    if st.session_state.current_step >= 1 :
        with st.expander("Step 1: Upload File & Configuration", expanded=st.session_state.current_step == 1):

            uploaded_file = st.file_uploader("Upload Excel File", type=["xlsx", "xls"])
            col1, col2 = st.columns(2)
            with col1:
                collections = get_collections(db)
                collection = st.selectbox("Select Target Collection", collections)
                st.session_state.collection = collection
            with col2:
            
                if collection and uploaded_file:
                    try:
                        df = read_excel_file(uploaded_file)
                        df = clean_dataframe(df)
                        st.session_state.df = df
                        st.success("File successfully uploaded and preprocessed!")
                        if st.button("Next Step → 2"):
                            st.session_state.current_step = 2
                            st.rerun()
                    except Exception as e:
                        st.error(f"Error processing file: {e}")

    # ---------------------------
    # Step 2: Process Dependencies
    # ---------------------------
    # ---------------------------
# Step 2: Process Dependencies
# ---------------------------
    if st.session_state.current_step >= 2:
        # retrieve the collection chosen in Step 1
        collection = st.session_state.get("collection")
        if not collection:
            st.error("Please select a collection in Step 1 first.")
        else:
            with st.expander("Step 2: Process Dependencies", expanded=(st.session_state.current_step == 2)):
                st.write("▶️ Entering dependency processor for collection:", collection)

                # debug: show what columns we actually have
                # st.write("ℹ️ DataFrame columns:", st.session_state.df.columns.tolist())

                # only attempt visitor_information lookups if those columns exist
                if collection == "visitor_information":
                    df = st.session_state.df

                    # derive lookup_company
                    if "company_name_en" in df.columns or "company_name_th" in df.columns:
                        df["lookup_company"] = df["company_name_en"].fillna("").replace("", pd.NA)
                        df["lookup_company"].fillna(df["company_name_th"].fillna(""), inplace=True)
                    else:
                        st.warning("Skipping lookup_company: no 'company_name_en' or 'company_name_th' column found.")


                    # derive position_at_work if lookup_position/department exist
                    if {"lookup_position", "lookup_department"}.issubset(df.columns):
                        df['position_at_work'] = df.apply(make_position_at_work, axis=1)
                    else:
                        st.warning("Skipping position_at_work: missing 'lookup_position' or 'lookup_department'.")

                    # For each extension 1 and 2:
                    for i in [1, 2]:
                        num_col = f"telephone_no_{i}"
                        ext_col = f"extension_{i}"
                        out_col = f"telephone_extension_{i}"

                        # Only do it if at least one source column exists
                        if num_col in df.columns or ext_col in df.columns:
                            # make sure both columns exist, else create a series of NaNs
                            nums = df[num_col] if num_col in df.columns else pd.Series([pd.NA]*len(df), index=df.index)
                            exts = df[ext_col] if ext_col in df.columns else pd.Series([pd.NA]*len(df), index=df.index)

                            # vectorized concat + strip
                            df[out_col] = (
                                nums.fillna('')
                                    .astype(str)
                                + '-'
                                + exts.fillna('')
                                    .astype(str)
                            ).str.strip('-') \
                            .replace('', None)  # turn empty back into None

                        # combined phone_number
                        phone_cols = [c for c in ["mobile_no_1","mobile_no_2","telephone_extension_1","telephone_extension_2"] if c in df.columns]

                        if phone_cols:
                            df['phone_number'] = df[phone_cols] \
                                .agg(lambda x: '|'.join(i for i in x.dropna().astype(str) if i.strip()), axis=1) \
                                .replace('', None)
                
                        else:
                            st.warning("Skipping phone_number: no phone columns found.")

                        st.session_state.df = df

                st.info("🔄 Automatically processing required dependencies...")
                st.info("🔄 dependencies origin_source")
                # define dependencies (only include those with existing source columns)
                dependencies = {}
                if "origin_source" in st.session_state.df.columns:
                    dependencies["original_source"] = {
                        "cols": ["origin_source"],
                        "mapping": {"origin_source": "source_name"}
                    }
                st.info("🔄 dependencies email")
                if "email" in st.session_state.df.columns:
                    dependencies["email_list"] = {
                        "cols": ["email"],
                        "mapping": {"email": "email_list"}
                    }
                st.info("🔄 dependencies phone_number")
                df_clone = st.session_state.df
                if "phone_number" in df_clone.columns:
                  
                    # Split and explode only if there's a "|" present

                    
                    df_clone["phone_number"] = df_clone["phone_number"].astype(str).str.split("|")
                    df_clone = df_clone.explode("phone_number").reset_index(drop=True)

                    # (Optional) Remove extra spaces
                    df_clone["phone_number"] = df_clone["phone_number"].str.strip()

                    # Now update dependencies if needed
                    dependencies["calling_list"] = {
                        "cols": ["phone_number"],
                        "mapping": {"phone_number": "number_with_extension"}
                    }
                st.info("🔄 dependencies company")
                df_clone_cp = st.session_state.df
                if "lookup_company" in df_clone_cp.columns:
                    df_clone_cp["lookup_company"] = df_clone_cp["lookup_company"].astype(str).str.split("|")
                    df_clone_cp = df_clone_cp.explode("lookup_company").reset_index(drop=True)
                    # (Optional) Remove extra spaces
                    df_clone_cp["lookup_company"] = df_clone_cp["lookup_company"].str.strip()

                    # company & position always present if visitor_information
                    dependencies["company"] = {
                        "cols": ["lookup_company",
                            "company_name_en","company_name_th","company_email",
                            "company_website","company_facebook","company_register_capital",
                            "company_employee_no","company_product_profile",
                            "lookup_industry","lookup_sub_industry"
                        ],
                        "mapping": {
                            "lookup_company": "company_name_code",
                            "company_register_capital":"register_capital",
                            "company_employee_no":"company_employee_size",
                            "lookup_industry":"industry",
                            "lookup_sub_industry":"sub_industry"
                        }
                    }
                st.info("🔄 dependencies position_at_work")
                df_clone_paw = st.session_state.df
                if "position_at_work" in df_clone_paw.columns:
                    df_clone_paw["position_at_work"] = df_clone_paw["position_at_work"].astype(str).str.split("|")
                    df_clone_paw = df_clone_cp.explode("position_at_work").reset_index(drop=True)

                    # (Optional) Remove extra spaces
                    df_clone_paw["position_at_work"] = df_clone_paw["position_at_work"].str.strip()
                    dependencies["position_at_work"] = {
                        "cols": ["position_at_work",
                            "lookup_position","lookup_department","lookup_company",
                            "lookup_industry","lookup_sub_industry"
                        ],
                        "mapping": {
                            "lookup_position":"position",
                            "lookup_department":"department",
                            "lookup_company":"company"
                        }
                    }
                st.info("🔄 processed_deps not in st.session_state")
                # initialize trackers
                if 'processed_deps' not in st.session_state:
                    st.session_state.processed_deps = {name: False for name in dependencies}
                else:
                    # ensure new keys are initialized
                    for name in dependencies:
                        st.session_state.processed_deps.setdefault(name, False)
                if 'dep_errors' not in st.session_state:
                    st.session_state.dep_errors = {}
                if 'dep_counts' not in st.session_state:
                    st.session_state.dep_counts = {}

                # progress display
                total = len(dependencies)
                done = sum(st.session_state.processed_deps.values())
                prog = st.progress(done / total)
                for name in dependencies:
                    status = (
                        "✅" if st.session_state.processed_deps[name]
                        else "❌" if name in st.session_state.dep_errors
                        else "⏳"
                    )
                    label = name.replace("_"," ").title()
                    if st.session_state.processed_deps[name]:
                        label += f" — {st.session_state.dep_counts.get(name,0):,} rows"
                    c1,c2 = st.columns([2,8])
                    with c1:
                        st.markdown(f"{status} **{label}**")
                    with c2:
                        if name in st.session_state.dep_errors:
                            st.error(st.session_state.dep_errors[name]["message"])
                        else:
                            st.caption("Completed" if st.session_state.processed_deps[name] else "Pending...")

                # auto‐process one dependency at a time
                if not all(st.session_state.processed_deps.values()):
                    for dep_name, cfg in dependencies.items():
                        if not st.session_state.processed_deps[dep_name] and dep_name not in st.session_state.dep_errors:
                            st.write(f"• Processing: {dep_name}")
                            try:
                                success, error_df, count = process_dependency(
                                    db, dep_name, cfg, st.session_state.df
                                )
                                st.write(f"  → success={success}, rows={count}")
                                if success:
                                    st.session_state.processed_deps[dep_name] = True
                                    st.session_state.dep_counts[dep_name] = count
                                    st.session_state.dep_errors.pop(dep_name, None)
                                    prog.progress(sum(st.session_state.processed_deps.values()) / total)
                                    st.rerun()
                            except Exception as e:
                                st.write(f"  ⚠️ Exception: {e}")
                                st.session_state.dep_errors[dep_name] = {
                                    "message": str(e),
                                    "error_df": error_df
                                }
                                st.rerun()
                            break

                # show error details
                if st.session_state.dep_errors:
                    with st.expander("🛑 Error Details", expanded=True):
                        for name, info in st.session_state.dep_errors.items():
                            if st.button(f"View errors for {name}", key=f"err_{name}"):
                                st.dataframe(info["error_df"], use_container_width=True)

                # next step
                if all(st.session_state.processed_deps.values()):
                    st.success("All dependencies processed successfully!")
                    if st.button("Next Step → 3"):
                        st.session_state.current_step = 3
                        st.rerun()


    # Step 3: Validate Data
    if st.session_state.current_step >= 3 :
        with st.expander("Step 3: Validate Data", expanded=st.session_state.current_step == 3):
            st.session_state.df = st.session_state.df.where(st.session_state.df != "", None)
            field_defs = get_field_definitions(db, collection)
            df = resolve_extra_columns(st.session_state.df, field_defs)
            
            passed_df, error_df, _ = validate_dataframe(df, field_defs, db)
            passed_df = preprocess_foreign_keys(passed_df, field_defs, db)
            passed_df = preprocess_array_fields(passed_df, field_defs)
            passed_df = drop_relationship_fields(passed_df, field_defs)
            passed_df = passed_df.where(pd.notna(passed_df), None)

            tab1, tab2 = st.tabs([f"✅ Valid Data ({len(passed_df):,})", f"❌ Errors ({len(error_df):,})"])

            with tab1:
                st.dataframe(passed_df, use_container_width=True)
            with tab2:
                st.dataframe(error_df, use_container_width=True,hide_index=True)
            
            if st.button("Next Step → 4"):
                st.session_state.current_step = 4
                st.rerun()

    # Step 4: Upload Data
    if st.session_state.current_step >= 4 :
        with st.expander("Step 4: Final Upload", expanded=st.session_state.current_step == 4):
            # Get collection schema for first column
            field_defs = get_field_definitions(db, collection)
            collection_columns = list(field_defs.keys())
            first_column = collection_columns[0] if collection_columns else None
            
            upload_mode = st.radio("Upload Mode:", ["insert on duplicate update","insert", "update"], horizontal=True)
            
            if upload_mode == "update":
                pk = st.selectbox("Primary key column", options=collection_columns)
            elif upload_mode == "insert on duplicate update":
                # Auto-select first column but allow override
                conflict_column = st.selectbox(
                    "Conflict target column", 
                    options=collection_columns,
                    index=0  # Auto-select first column
                )
            
            if st.button("🔥 Start Final Upload"):
                with st.spinner("Uploading data..."):
                    # Use first_column if not overridden
                    final_conflict_col = conflict_column if upload_mode == "insert on duplicate update" else None
           
                    result = upload_data(db, collection, passed_df, upload_mode, 
                                      pk if upload_mode == "update" else None,
                                      final_conflict_col)
                    if result:
                        st.balloons()
                        st.success(f"✅ Successfully uploaded {len(passed_df)} rows!")
                    else:
                        st.error("❌ Upload failed. Please check the error logs.")

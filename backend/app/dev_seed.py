from pymysql.cursors import DictCursor


DEMO_DEPARTMENTS = (
    ("00000000-0000-4000-8000-000000000001", "Computer Science and Engineering", "CSE"),
    ("00000000-0000-4000-8000-000000000002", "Electronics and Communication", "ECE"),
    ("00000000-0000-4000-8000-000000000003", "Mechanical Engineering", "MECH"),
)

DEMO_PAPERS = (
    (
        "00000000-0000-4000-8000-000000000101",
        "Sample: Data Structures",
        "DEMO-CS201",
        "CSE",
        3,
        "CIA 1",
        "2025",
        "Demo listing for app preview, no PDF is attached.",
        None,
    ),
    (
        "00000000-0000-4000-8000-000000000105",
        "Sample: Campus-notes Demo Paper",
        "DEMO-PDF-001",
        "CSE",
        1,
        "Sample PDF",
        "2026",
        "A demo PDF for testing paper reading and download. Not an official exam paper.",
        "/uploads/sample-campus-notes.pdf",
    ),
    (
        "00000000-0000-4000-8000-000000000102",
        "Sample: Database Management Systems",
        "DEMO-CS301",
        "CSE",
        5,
        "End Semester",
        "2024",
        "Demo listing for app preview, no PDF is attached.",
        None,
    ),
    (
        "00000000-0000-4000-8000-000000000103",
        "Sample: Digital Electronics",
        "DEMO-EC201",
        "ECE",
        3,
        "CIA 2",
        "2025",
        "Demo listing for app preview, no PDF is attached.",
        None,
    ),
    (
        "00000000-0000-4000-8000-000000000104",
        "Sample: Engineering Thermodynamics",
        "DEMO-ME201",
        "MECH",
        3,
        "Weekly Test 1",
        "2024",
        "Demo listing for app preview, no PDF is attached.",
        None,
    ),
)


def seed_demo_data(connection) -> int:
    inserted = 0
    with connection.cursor(DictCursor) as cursor:
        department_ids = {}
        for department_id, name, code in DEMO_DEPARTMENTS:
            cursor.execute("SELECT id FROM departments WHERE code = %s", (code,))
            department = cursor.fetchone()
            if department is None:
                cursor.execute(
                    """
                    INSERT INTO departments (id, name, code, created_at, updated_at)
                    VALUES (%s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    (department_id, name, code),
                )
                department = {"id": department_id}
                inserted += 1
            department_ids[code] = department["id"]

        for paper in DEMO_PAPERS:
            paper_id, subject, subject_code, department_code, semester, exam_type, year, description, file_url = paper
            cursor.execute(
                "SELECT id FROM papers WHERE id = %s OR subject_code = %s LIMIT 1",
                (paper_id, subject_code),
            )
            if cursor.fetchone() is not None:
                continue
            cursor.execute(
                """
                INSERT INTO papers (
                    id, subject, subject_code, department_id, semester, exam_type,
                    year, description, file_url, created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """,
                (
                    paper_id,
                    subject,
                    subject_code,
                    department_ids[department_code],
                    semester,
                    exam_type,
                    year,
                    description,
                    file_url,
                ),
            )
            inserted += 1
    return inserted


def main() -> None:
    from .database import connect_mysql, verify_schema

    connection = connect_mysql()
    try:
        verify_schema(connection)
        inserted = seed_demo_data(connection)
        connection.commit()
        print(f"Demo seed complete; inserted {inserted} missing records.")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    main()

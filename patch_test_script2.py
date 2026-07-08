import sqlite3

def check_logic():
    wildcards = (None, 'None', '未知')
    known_classes = set(['香射手'])

    test_cls = '幻影劍士'

    is_valid = True
    if test_cls not in wildcards:
        if known_classes and test_cls not in known_classes:
            is_valid = False

    print(f"Is valid: {is_valid}")

check_logic()

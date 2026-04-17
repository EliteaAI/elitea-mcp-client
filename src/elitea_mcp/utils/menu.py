import os


def show_menu(tree={}):
    lines, positions = _flat_tree_to_menu_lines(tree)
    menu_indexes = list(positions.keys())

    print("----------------------------")
    print("Available Servers and Tools:")
    print("----------------------------")
    for idx, item in enumerate(lines):
        if idx in menu_indexes:
            print(f"  {item}")
        else:
            print(f"  [{idx + 1}]  {item}")

    print("\nEnter the numbers of the items you want to exclude, separated by commas (e.g., 1,3,5).")
    exclude_indices = input("(Leave blank and press Enter to select all and proceed): ").split(",")
    exclude_indices = {int(i.strip()) - 1 for i in exclude_indices if i.strip().isdigit()}

    selection = {}
    current_menu_item = None
    for idx, item in enumerate(lines):
        if idx in menu_indexes:
            current_menu_item = item
            selection[current_menu_item] = []
        elif idx not in exclude_indices and current_menu_item:
            selection[current_menu_item].append(item)

    # print result
    print("\n----------------------------")
    print("Servers and Tools to serve:")
    print("----------------------------")
    for server, tools in list(selection.items()):
        if tools:
            print(f"  {server}")
            for tool in tools:
                print(f"    {tool}")
            print("")
        else:    
            del selection[server]

    return selection

def _flat_tree_to_menu_lines(tree):
    lines = []
    toolkit_ids = {}
    cursor = 0
    for menu, sub_items in tree.items():
        lines.append(menu)
        lines.extend(sub_items)
        toolkit_ids[cursor] = list(range(cursor + 1, cursor + len(sub_items) + 1))
        cursor += len(sub_items) + 1
    return lines, toolkit_ids
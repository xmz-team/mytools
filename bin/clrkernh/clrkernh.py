#!/usr/bin/env python3
# clrkernh
# Version: 2.1
# Auther: AD <ad-ios334@outlook.com>
# Maintainer: XMZ <xmz-team@outlook.com>
import re
import sys
import argparse
import glob
import os
from pathlib import Path

def clean_header(content, force_cover=False, keep_includes=False):
    if force_cover:
        content = "" + content
    content = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)
    lines = content.split('\n')
    cleaned_lines = []
    for line in lines:
        # Decide whether to keep the #include line according to the keep_includes parameter
        if re.match(r'^\s*#include', line):
            if keep_includes:
                cleaned_lines.append(line)
            continue
        if '//' in line:
            in_string = False
            new_line = ''
            i = 0
            while i < len(line):
                if line[i] == '"' and (i == 0 or line[i-1] != '\\'):
                    in_string = not in_string
                    new_line += line[i]
                elif line[i:i+2] == '//' and not in_string:
                    break
                else:
                    new_line += line[i]
                i += 1
            line = new_line.rstrip()
        if line.strip():
            cleaned_lines.append(line)
    content = '\n'.join(cleaned_lines)
    content = re.sub(r'\n\s*\n\s*\n', '\n\n', content)
    return content

def process_file(input_path, output_path=None, keep_includes=False, force_cover=False, maintain_original_name=False):
    with open(input_path, 'r', encoding='utf-8') as f:
        content = f.read()
    cleaned = clean_header(content, force_cover, keep_includes)
    input_path_obj = Path(input_path)
    # Process the output path
    if output_path is None:
        if maintain_original_name and force_cover:
            # Use the original name mode and the overwrite mode, and use the original path directly
            output_path = input_path_obj
        else:
            # General mode, add the suffix _clean
            output_path = input_path_obj.parent / f"{input_path_obj.stem}_clean{input_path_obj.suffix}"
    else:
        # Make sure that the output_path is a path object
        if isinstance(output_path, str):
            output_path = Path(output_path)

        if output_path.is_dir() or str(output_path).endswith('/'):
            # The output is the directory.
            if maintain_original_name and force_cover:
                # Original name mode: use the original file name
                output_path = output_path / input_path_obj.name
            else:
                # General mode: add the _clean suffix
                output_path = output_path / f"{input_path_obj.stem}_clean{input_path_obj.suffix}"
        else:
            # The output is the complete file path.
            if maintain_original_name and force_cover:
                # The original name is the mode, but the complete output path is specified, and the specified path is used directly.
                pass
    # Make sure that output_path is a Path object
    if not isinstance(output_path, Path):
        output_path = Path(output_path)
    # Create a parent directory
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Write to the file
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(cleaned)

    action = "Cover" if output_path == input_path_obj else "Clear"
    print(f"Already{action}: {input_path} -> {output_path}")
    return output_path

def process_multiple_files(input_patterns, output_dir=None, keep_includes=False, force_cover=False, maintain_original_name=False):
    files_processed = []
    for pattern in input_patterns:
        matching_files = glob.glob(pattern, recursive=True)
        if not matching_files:
            print(f"[Warn]: Unable to find the file matching '{pattern}'")
            continue
        for input_path in matching_files:
            try:
                output_path = None
                if output_dir:
                    output_path = output_dir
                process_file(input_path, output_path, keep_includes, force_cover, maintain_original_name)
                files_processed.append(input_path)
            except Exception as e:
                print(f"An error occurred when processing the file {input_path}: {e}")
                import traceback
                traceback.print_exc()  #Add detailed error messages for debugging
    return files_processed

def main():
    global args
    parser = argparse.ArgumentParser(description='Clean up the C/C++ header, remove annotations and #include, etc.')
    parser.add_argument('input', nargs='+', help='Enter the header file path (supports wildcards, such as *.h or **/*.h)')
    parser.add_argument('-o', '--output', help='Output directory (preset to the directory where each file is located)')
    parser.add_argument('--keep-includes', '-ki', action='store_true', help='Keep #include lines (removed by default)')
    parser.add_argument('--recursive', '-r', action='store_true', 
                       help='Recursive search subdirectory (used with **/*.h)')
    parser.add_argument('-f-cov', '--force-cover', action='store_true', 
                       help='Forced overlay mode, add overlay marks at the beginning of the file')
    parser.add_argument('-fcov', action='store_true', dest='force_cover', 
                       help='-f-cov  alias')
    parser.add_argument('--maintain-original-name', '-m-orig-n', '-m-orign', '-mon', 
                       action='store_true', dest='maintain_original_name',
                       help='Keep the original file name and do not add the _clean suffix (only available in overlay mode)')
    args = parser.parse_args()

    try:
        # Parameter verification: The original name option can only be used in overlay mode.
        if args.maintain_original_name and not args.force_cover:
            print("[Error]:The --maintain-original-name option can only be used in --force-cover mode.")
            print("Because maintaining the original name will cover the original file, the forced overwriting mode must be clearly enabled.")
            return 1

        if args.recursive:
            expanded_patterns = []
            for pattern in args.input:
                if '**' not in pattern and '*' in pattern:
                    pattern = f"**/{pattern}"
                expanded_patterns.append(pattern)
        else:
            expanded_patterns = args.input

        files_processed = process_multiple_files(
            expanded_patterns, 
            args.output, 
            args.keep_includes,
            args.force_cover,
            args.maintain_original_name
        )

        if files_processed:
            print(f"\nSuccessfully processed {len(files_processed)} files")
            if args.force_cover:
                print("Forced override mode is enabled")
            if args.maintain_original_name:
                print("The original name mode has been enabled (the original file will be overwritten)")
            if args.keep_includes:
                print("#include lines are preserved")
        else:
            print("No files have been processed.")
            return 1

    except Exception as e:
        print(f"[Error]: {e}")
        import traceback
        traceback.print_exc()
        return 1
    return 0

if __name__ == '__main__':
    sys.exit(main());

# This is a gadget for processing kernel header files.
# His function is to remove annotations, cited headers, etc, In the end, only the definition is left.
# Note: This gadget may not be able to handle specific scenarios well, but it is still applicable to most scenarios.

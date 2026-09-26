#!/bin/sh -e
#psoatop 'Perform specific operations according'

unset jb
jbenv="$(dpkg --print-architecture)"
if [ "$jbenv" = "iphoneos-arm64" ]; then
    jb="/var/jb"
elif [ "$jbenv" = "iphoneos-arm64e" ]; then
    jb="$(jbroot)"
else
    jb=""
fi

mkdir -p "$HOME/.config/psoatop/scripts"
config_file="$HOME/.config/psoatop/psoatop.io.github.xmz-team.json"

if [ ! -f "$config_file" ]; then
    cat > "$config_file" << 'ENDOFFILE'
{
    "annotate": "This is a list, and the suite in the list will customize the script by itself",
    "packages_id": {}
}
ENDOFFILE
fi

. "$jb/etc/profile" 1>/dev/null 2>&1 || true
script_path="$HOME/.config/psoatop/scripts"

add_namelist() {
    packageid="$1"
    script_name="$2"
    if [ -z "$packageid" ] || [ -z "$script_name" ]; then
        echo "Usage: $0 add <package_id> <script_name>" >&2
        return 1
    fi
    jq --arg pkg "$packageid" --arg script "$script_name" \
       '.packages_id[$pkg] = $script' "$config_file" > "${config_file}.tmp" \
       && mv "${config_file}.tmp" "$config_file"
    echo "Added: $packageid -> $script_name"
}

remove_namelist() {
    packageid="$1"
    if [ -z "$packageid" ]; then
        echo "Usage: $0 remove <package_id>" >&2
        return 1
    fi
    jq --arg pkg "$packageid" 'del(.packages_id[$pkg])' "$config_file" > "${config_file}.tmp" \
       && mv "${config_file}.tmp" "$config_file"
    echo "Removed: $packageid"
}

show_help() {
    cat << ENDOFHELP
Usage: $0 [options]
   add <package_id> <script_name>    # add packageid to list
   remove <package_id>               # remove packageid in list
   help, h                           # show this help
ENDOFHELP
}

main() {
    case $1 in
        ""|"h"|"help") show_help ;;
        add) add_namelist "$2" "$3" ;;
        remove|rm) remove_namelist "$2" ;;
        *) echo "unknown option: $1" >&2; show_help ;;
    esac
}

main "$@"


#!/bin/sh -e
unset jb
jbdpkgarch="$(dpkg --print-architecture)";

if [ "$jbdpkgarch" = "iphoneos-arm64" ]; then
    jb="/var/jb";
elif [ "$jbdpkgarch" = "iphoneos-arm64e" ]; then
    jb="$(jbroot)";
else
    jb="";
fi

export jb;

if [ "$1" = "upgrade" ]; then
    exit 0;
fi

rm -f "$jb/usr/bin/psoatop"
rm -f "$HOME/.config/psoatop/psoatop.io.github.xmz-team.json"
rm -rf "$HOME/.config/psoatop/scripts/*"

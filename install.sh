#!/bin/sh
# Install ostack9s from the GitHub releases, without sudo.
#
#   curl -fsSL https://github.com/rizlas/ostack9s/releases/latest/download/install.sh | sh
#
# Environment:
#   VERSION  release to install, e.g. 0.1.0 (default: latest)
#   PREFIX   install directory (default: ~/.local/bin)
set -eu

REPO="rizlas/ostack9s"
PREFIX="${PREFIX:-$HOME/.local/bin}"

die() {
    echo "ostack9s install: $*" >&2
    exit 1
}

command -v curl >/dev/null 2>&1 || die "curl is required"
command -v tar >/dev/null 2>&1 || die "tar is required"

case "$(uname -s)" in
    Linux) os=linux ;;
    Darwin) os=darwin ;;
    *) die "unsupported system: $(uname -s) (Linux and macOS only)" ;;
esac

case "$(uname -m)" in
    x86_64 | amd64) arch=x86_64 ;;
    aarch64 | arm64) [ "$os" = darwin ] && arch=arm64 || arch=aarch64 ;;
    *) die "unsupported architecture: $(uname -m)" ;;
esac

if [ -z "${VERSION:-}" ]; then
    latest=$(curl -fsSLI -o /dev/null -w '%{url_effective}' \
        "https://github.com/$REPO/releases/latest")
    VERSION="${latest##*/v}"
    [ -n "$VERSION" ] || die "cannot find the latest release"
fi
VERSION="${VERSION#v}"

name="ostack9s-$VERSION-$os-$arch"
base="https://github.com/$REPO/releases/download/v$VERSION"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

echo "Downloading $name"
curl -fsSL -o "$tmp/$name.tar.gz" "$base/$name.tar.gz" ||
    die "no binary for $os/$arch in release v$VERSION"
curl -fsSL -o "$tmp/SHA256SUMS" "$base/SHA256SUMS" || die "cannot download SHA256SUMS"

expected=$(grep " $name.tar.gz\$" "$tmp/SHA256SUMS" | cut -d' ' -f1)
[ -n "$expected" ] || die "$name.tar.gz is not listed in SHA256SUMS"
if command -v sha256sum >/dev/null 2>&1; then
    actual=$(sha256sum "$tmp/$name.tar.gz" | cut -d' ' -f1)
else
    actual=$(shasum -a 256 "$tmp/$name.tar.gz" | cut -d' ' -f1)
fi
[ "$expected" = "$actual" ] || die "checksum mismatch for $name.tar.gz"

tar -xzf "$tmp/$name.tar.gz" -C "$tmp"
mkdir -p "$PREFIX"
cp "$tmp/$name/ostack9s" "$PREFIX/ostack9s.tmp"
chmod 0755 "$PREFIX/ostack9s.tmp"
mv -f "$PREFIX/ostack9s.tmp" "$PREFIX/ostack9s"
echo "Installed $("$PREFIX/ostack9s" --version) to $PREFIX/ostack9s"

case ":$PATH:" in
    *":$PREFIX:"*) ;;
    *)
        echo
        echo "$PREFIX is not in your PATH. Add this line to ~/.zshrc or ~/.bashrc:"
        echo "  export PATH=\"$PREFIX:\$PATH\""
        ;;
esac

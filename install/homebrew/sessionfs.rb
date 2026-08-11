# Homebrew formula for SessionFS — https://sessionfs.dev
#
# This file is kept in the main repo for review and versioning.  The tap repo
# (SessionFS/homebrew-tap) is created at publish time by the operator and
# contains the published copy of this formula.
#
# To publish a new version:
#   1. Update version + sha256 below (fetch the sdist sha256 from PyPI):
#        curl -s https://pypi.org/pypi/sessionfs/json | python3 -c "
#        import json, sys
#        r = json.load(sys.stdin)
#        for f in r['releases']['X.Y.Z']:
#            if f['packagetype']=='sdist': print(f['digests']['sha256'])"
#   2. Run `brew update-python-resources sessionfs` to regenerate the resource
#      stanzas pinned to the virtualenv install.  Commit the result.
#   3. Push to SessionFS/homebrew-tap.
#
# Users install with:
#   brew tap sessionfs/tap
#   brew install sessionfs
class Sessionfs < Formula
  include Language::Python::Virtualenv

  desc "Portable session layer for AI coding tools"
  homepage "https://sessionfs.dev"
  url "https://files.pythonhosted.org/packages/c7/63/1604f5c383fae02af6b078d717e0ac8ce2c590005788825238d61a2f778a/sessionfs-0.14.0.tar.gz"
  sha256 "df8c84a9ade22573fe12554cd19152a14b7ad3df97af42b6f2db5e1dff4e66c3"
  license "MIT"

  depends_on "python@3.12"

  # Resource stanzas below are regenerated at publish time via
  # `brew update-python-resources sessionfs`.  Do not hand-edit.
  # The list here is a placeholder — the tap repo formula carries
  # the full pinned set.

  def install
    virtualenv_install_with_resources

    # Symlink the daemon alongside the CLI.
    bin.install_symlink libexec/"bin/sfsd"
  end

  test do
    assert_match "SessionFS", shell_output("#{bin}/sfs --help")
  end
end

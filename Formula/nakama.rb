class Nakama < Formula
  desc "Run a crew of coding agents in herdr panes, one git worktree each"
  homepage "https://github.com/anuress/nakama"
  url "https://github.com/anuress/nakama/archive/refs/tags/v0.1.6.tar.gz"
  sha256 "b40acb2be8d96b56a3b592cb0299fedbdb3a9876fff8f34e3c5c4a25d2aebc33"
  license "MIT"
  head "https://github.com/anuress/nakama.git", branch: "master"

  depends_on "herdr"
  depends_on "worktrunk"

  def install
    # nakama finds its roles/ relative to bin/nakama, so keep the tree together in libexec.
    libexec.install "bin", "roles", "skills", "install.sh"
    bin.install_symlink libexec/"bin/nakama"
  end

  def caveats
    <<~EOS
      Link the skills into your agents (pi, Claude Code):
        #{opt_libexec}/install.sh --skills-only

      Each agent CLI also needs herdr's integration:
        herdr integration install pi
        herdr integration install claude

      Before `brew uninstall nakama`, remove the links:
        #{opt_libexec}/install.sh --uninstall --skills-only
    EOS
  end

  test do
    assert_match(/^nakama \d/, shell_output("#{bin}/nakama --version"))
    assert_match "builder", shell_output("#{bin}/nakama roles")
  end
end

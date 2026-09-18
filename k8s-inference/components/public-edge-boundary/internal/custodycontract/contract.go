package custodycontract

import (
	"path/filepath"
	"strings"
)

const MaximumDirectoryDepth = 2

func ValidDirectoryDestination(path string) bool {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path || !strings.HasPrefix(path, "/custody/") || strings.ContainsAny(path, "\x00\r\n") {
		return false
	}
	relative := strings.TrimPrefix(path, "/custody/")
	components := strings.Split(relative, "/")
	if len(components) < 1 || len(components) > MaximumDirectoryDepth {
		return false
	}
	for _, component := range components {
		if component == "" || component == "." || component == ".." {
			return false
		}
	}
	return true
}

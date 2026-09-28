#define main unused_device_helper_main
#include "../vendor/airlift/Sources/device_helper.m"
#undef main
#include <assert.h>

int main(void) {
    @autoreleasepool {
        TargetIdentifier = CFSTR("fixture-device");
        NSDictionary *summary = @{ @"productType": @"iPhone99,1", @"productVersion": @"99.0", @"buildVersion": @"99A1" };
        BOOL tested = YES;
        unsetenv("CARRIER_EXPERIMENTAL_TARGET");
        assert(!TargetGate(summary, &tested));
        assert(!tested);
        setenv("CARRIER_EXPERIMENTAL_TARGET", "{\"device\":\"fixture-device\",\"ProductType\":\"iPhone99,1\",\"ProductVersion\":\"99.0\",\"BuildVersion\":\"99A1\"}", 1);
        assert(TargetGate(summary, &tested));
        assert(!tested);
        NSDictionary *changed = @{ @"productType": @"iPhone99,1", @"productVersion": @"99.0", @"buildVersion": @"99B2" };
        assert(!TargetGate(changed, &tested));
        TargetIdentifier = CFSTR("other-device");
        assert(!TargetGate(summary, &tested));
        setenv("CARRIER_EXPERIMENTAL_TARGET", "{}", 1);
        assert(!TargetGate(summary, &tested));
        unsetenv("CARRIER_EXPERIMENTAL_TARGET");
        NSDictionary *known = @{ @"productType": @"iPhone18,2", @"productVersion": @"27.2", @"buildVersion": @"24B5084k" };
        assert(TargetGate(known, &tested));
        assert(tested);
        puts("Native target gate tests passed without device access");
    }
    return 0;
}
